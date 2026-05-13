# JobJarvis — Full System Audit Report
**Auditor:** Principal Staff Engineer + AI Systems Architect  
**Date:** 2026-05-04  
**Scope:** Complete backend codebase — all 85 Python files  
**Status:** AUDIT COMPLETE — Do not implement until sign-off

---

## Executive Summary

JobJarvis has a well-conceived architecture but contains **1 guaranteed runtime crash** that makes every Celery scan task fail on execution, **5 silent logic bugs** that produce incorrect data and wrong AI decisions, **2 schema mismatches** between ORM and migration, and **multiple N+1 query patterns** that will cause production degradation at scale. The platform cannot function correctly in production as-is.

---

## Module Map

```
backend/app/
├── config.py                   ← Settings (pydantic-settings, all env-overridable)
├── database.py                 ← SQLAlchemy async engine, init_db(), SQLite+PG dual mode
├── main.py                     ← FastAPI lifespan, router mounts, realtime monitor start
│
├── models/
│   ├── user.py                 ← User, UserRole
│   ├── company.py              ← Company (ATS config, scheduling, 40K registry)
│   ├── job.py                  ← Job, JobStatusHistory (BigIntPK, full enrichment schema)
│   ├── resume.py               ← ResumeVersion (parsed structured data)
│   ├── application.py          ← Application, ApplicationAnswer, OutreachMessage
│   └── ai_models.py            ← AIDecision, AIMemory, JobEmbedding, ResumeEmbedding,
│                                  CompanyIntelligence, ScanRun, BronzeRawJob, etc.
│
├── api/v1/
│   ├── auth.py / jobs.py / companies.py / applications.py
│   ├── agent.py / resumes.py / scans.py / reports.py / outreach.py
│   └── health.py / admin.py
│
├── ai/
│   ├── decision_engine.py      ← DB-backed fit scoring + decision (used by API routes)
│   ├── decision_agent.py       ← Pure-function decision logic (used by CareerAgent)
│   ├── resume_matcher.py       ← Multi-dimensional match computation
│   ├── skill_extractor.py      ← Regex catalog-based extraction
│   ├── role_classifier.py      ← Pattern-count role category classifier
│   ├── spam_detector.py        ← Rule-based spam scoring
│   ├── work_auth_detector.py   ← Restriction flag detector
│   ├── source_classifier.py    ← DIRECT_COMPANY / STAFFING / CONSULTING classifier
│   ├── interview_probability.py← Bayesian probability estimator
│   ├── learning_engine.py      ← Skill gap analysis from historical decisions
│   ├── recruiter_writer.py
│   └── agent/
│       ├── career_agent.py     ← Main Observe→Analyze→Decide→Act→Learn loop
│       ├── evaluator.py        ← Outcome quality assessment
│       ├── memory_store.py     ← AIMemory CRUD + scoring adjustments
│       ├── planner.py          ← Weekly plan generation
│       └── self_corrector.py   ← Feedback-based weight corrections
│
├── services/
│   ├── job_pipeline.py         ← Pipeline A: bulk upsert, no Bronze layer (scheduler/direct)
│   ├── normalizer.py           ← Location/role/skill/title normalization
│   ├── dedup.py                ← DedupEngine (3-level), compute_fingerprint
│   ├── embedding_service.py    ← Zero-vector stub (Phase 1)
│   ├── freshness.py            ← Age-based freshness labels
│   ├── ranking.py              ← Gold-layer composite rank score
│   ├── data_quality.py         ← Quality report generation
│   ├── auto_apply.py           ← Auto-apply engine (rate-limited, strategy-based)
│   ├── ai_cost_control.py      ← LLM gate + usage logging
│   ├── realtime_monitor.py     ← Pipeline B: watermark-based 50s delta fetch
│   ├── scheduler.py            ← APScheduler wrapper for run_ingestion_pipeline()
│   ├── compliance.py           ← robots.txt + blocklist + FetchAuditLog
│   └── [other services]
│
├── connectors/
│   ├── base.py                 ← BaseConnector (retry, backoff, rate limit, RawJob, ConnectorResult)
│   ├── ats.py                  ← HTTP-level ATS dispatcher (Greenhouse/Lever/Ashby/Workday/SR)
│   ├── greenhouse.py / lever.py / ashby.py / smartrecruiters.py / workday.py / icims.py
│   └── aggregator.py           ← Aggregator fallback
│
└── workers/
    ├── celery_app.py           ← Celery config + Beat schedule (5 periodic tasks)
    ├── scan_tasks.py           ← Pipeline C: Celery scan → Bronze → Silver → Gold
    └── ai_tasks.py             ← CareerAgent, data quality, company intelligence tasks
```

---

## Data Flow

```
INGESTION (3 parallel paths — this is a problem):

Path A: Celery Beat → scan_tier_companies → run_company_scan_task
        → _scan_company_async() [scan_tasks.py]
        → ATS connector (BaseConnector classes)
        → Bronze BronzeRawJob write
        → DedupEngine.upsert_job() [one-by-one, N+1]
        → Silver Job row
        ⚠ CRASHES on every run: normalize_job() doesn't exist

Path B: FastAPI lifespan → start_realtime_monitor()
        → realtime_monitor.py every 50s
        → fetch_jobs_from_ats (connectors/ats.py)
        → ON CONFLICT DO NOTHING bulk insert
        ⚠ Missing fingerprint, source_type in insert dict

Path C: scheduler.py / test scripts
        → run_ingestion_pipeline() [job_pipeline.py]
        → fetch_jobs_from_ats / aggregator fallback
        → Bulk ON CONFLICT DO UPDATE
        ✅ Best implementation — but not triggered by Celery Beat

PROCESSING:

CareerAgent (Celery every 15min):
  Observe: Load jobs (last 24h, unprocessed, limit 200) — no user filter ⚠
  Analyze: _analyze_job() → job_dict, resume_profile (tuple, annotated as dict ⚠)
  Decide:  compute_match() + make_decision() → AIDecision row
  Learn:   SelfCorrector → MemoryStore writes

Decision Engine (API-level, per-request):
  evaluate_job_decision() → calculate_fit_score() → AIDecision row
  (Separate scoring model from CareerAgent's compute_match — two scoring systems)

STORAGE:

  jobs table → embedding_service → job_embeddings (zero-vector stub)
  AIDecision.decision = DecisionType string value
  AIMemory = persistent scoring adjustments
  FetchAuditLog = compliance audit trail
  ScanRun + BronzeRawJob = ingestion audit trail (Path A only)
```

---

## All Async Boundaries

| Boundary | Safe? | Notes |
|---|---|---|
| `celery_app` → `scan_tasks._run_async()` | ✅ Fixed | `asyncio.run()` correct |
| `celery_app` → `ai_tasks._run_async()` | ✅ Fixed | `asyncio.run()` correct |
| FastAPI lifespan → `init_db()` | ✅ | Standard async |
| FastAPI lifespan → `start_realtime_monitor()` | ✅ | asyncio.Task |
| `run_ingestion_pipeline()` → `asyncio.gather()` | ✅ | Semaphore-bounded |
| `DedupEngine.upsert_job()` | ✅ Correct but | One DB round-trip per job |
| `career_agent.run()` → `db.commit()` inside loop | ⚠ | `flush()` + `commit()` split — safe but redundant |
| `scheduler.py` → `asyncio.run()` | ✅ | Standalone process |

---

## All DB Interactions

| Location | Pattern | Issue |
|---|---|---|
| `job_pipeline.py` upsert | Bulk ON CONFLICT DO UPDATE | ✅ Best practice |
| `scan_tasks.py` upsert | `DedupEngine.upsert_job` per row | ⚠ N+1 |
| `realtime_monitor.py` insert | Bulk ON CONFLICT DO NOTHING | ✅ Correct |
| `career_agent._observe()` | 2 queries + Python filter | ⚠ N+1 pattern |
| `planner.generate_weekly_plan()` | Loop `select(Job).where(id==...)` | ⚠ N+1 (5 queries) |
| `decision_engine.evaluate_job_decision()` | 2 extra queries per call | ⚠ Batch needed |
| `data_quality.run_quality_report()` | 6 separate COUNT queries | ⚠ Could be 1 query |
| `ai_tasks._update_intelligence()` | 2 queries per company × 1000 | ⚠ 2000 queries |

---

---

# ISSUE REGISTRY

---

## 🔴 P0 — RUNTIME CRASHES (Guaranteed failures on execution)

### P0-1 · `normalize_job` function does not exist — EVERY Celery scan crashes
**File:** `app/workers/scan_tasks.py:82`  
**Severity:** CRASH — `ImportError` on every task execution

```python
# scan_tasks.py line 82 — inside _scan_company_async():
from app.services.normalizer import normalize_job   # ← DOES NOT EXIST
...
normalized = normalize_job(raw_job)   # line 180 — never reached
```

`normalizer.py` exports: `normalize_title`, `normalize_location`, `parse_location`, `normalize_country`, `normalize_remote`, `normalize_currency`, `classify_experience_level`, `classify_role_category`, `normalize_skill`.  
**`normalize_job` is not in this list.** It was never written.

**Impact:** Every `run_company_scan_task` Celery job crashes before doing any work. The Bronze→Silver→Gold pipeline (Path A) is completely non-functional. Celery Beat fires this every hour/6h/daily — all fail silently.

**Fix required:** Write `normalize_job(raw_job: RawJob) -> dict` in `normalizer.py` that maps `RawJob` fields to the dict schema expected by `_scan_company_async`.

---

### P0-2 · `_analyze_job` return type annotation lies — misleads tooling
**File:** `app/ai/agent/career_agent.py:93`  
**Severity:** Wrong annotation (works at runtime but breaks type safety)

```python
async def _analyze_job(self, job: Job, resume: Optional[ResumeVersion]) -> dict:
    ...
    return job_dict, resume_profile   # returns TUPLE, not dict
```

The caller correctly unpacks: `job_dict, resume_profile = await self._analyze_job(...)`.  
But the annotation says `-> dict`, which confuses mypy, IDEs, and any future caller.

**Fix required:** Change annotation to `-> tuple[dict, dict]`.

---

## 🔴 P1 — SILENT LOGIC BUGS (Wrong data, wrong AI behavior, wrong decisions)

### P1-1 · `success_rate` formula always produces 0.2 — learning signal broken
**File:** `app/ai/decision_engine.py:448`  
**Severity:** SILENT BUG — learning system receives permanently wrong signal

```python
user_stats = {
    "success_rate": total_interviews / max(1, total_interviews * 5),
    # When total_interviews=10: 10 / max(1, 50) = 10/50 = 0.20 ← always
    # When total_interviews=1:  1  / max(1,  5) =  1/5  = 0.20 ← always
}
```

The denominator `total_interviews * 5` is derived from the same variable as the numerator. When `total_interviews > 0` the result is always exactly `1/5 = 0.2`. The formula is supposed to represent the interview-to-application ratio (success rate), but as written it's a constant that never reflects actual user performance.

**Fix required:** Track total applications separately, compute `total_interviews / max(1, total_applications)`.

---

### P1-2 · Celery scan pipeline stores source classifier type in `source_type` field — API/auto-apply mismatch
**File:** `app/workers/scan_tasks.py:227`  
**Severity:** SILENT BUG — wrong values in DB, downstream filters break

```python
# scan_tasks.py — stores SourceClassification.source_type:
"source_type": source_cls.source_type,   # → "DIRECT_COMPANY" | "STAFFING_AGENCY"

# job_pipeline.py — stores ATS name:
"source_type": r_job.get("source", "ats"),  # → "greenhouse" | "lever" | "ats"

# auto_apply.py — classifies based on ATS name patterns:
if "workday" in url or "myworkdayjobs" in url: return "EXTERNAL"
elif source in ["greenhouse", "lever", "ashby"]: return "FORM_BASED"
elif source == "ats" or "easy_apply" in source: return "EASY_APPLY"
```

Jobs ingested via Celery (the production path) will have `source_type = "DIRECT_COMPANY"` while jobs ingested via the scheduler pipeline will have `source_type = "greenhouse"`. The auto-apply classifier checks for ATS names and will always classify Celery-ingested jobs as "EXTERNAL" (hardest to apply), even when they came from Greenhouse.

**Fix required:** Standardize `source_type` to ATS name across both pipelines. Store source classifier result in a separate `source_quality` field if needed.

---

### P1-3 · `city` and `region` columns missing from Alembic migration — silent write failure on PostgreSQL
**File:** `alembic/versions/001_initial_schema.py`  
**Severity:** SCHEMA MISMATCH — writes silently dropped on PostgreSQL

The `Job` ORM model defines:
```python
city: Mapped[Optional[str]] = mapped_column(String(100), index=True)
region: Mapped[Optional[str]] = mapped_column(String(100), index=True)
```

The Alembic migration for the `jobs` table does **not** include `city` or `region` columns. On PostgreSQL (production), every insert with `city` or `region` will fail with `column "city" does not exist` (or silently drop if using ON CONFLICT logic).

On SQLite, `database.py::_sqlite_add_missing_columns()` patches this at startup, masking the bug in development.

**Fix required:** Add `city` and `region` columns to the migration.

---

### P1-4 · `realtime_monitor.py` missing `fingerprint` and `source_type` in insert dict — dedup level 3 broken for realtime jobs
**File:** `app/services/realtime_monitor.py` (new_rows dict, ~line 245)  
**Severity:** SILENT BUG — dedup broken, source_type NULL

The `new_rows` dict built in `_fetch_delta()` is missing two critical fields:
- `fingerprint` → All realtime-inserted jobs have NULL fingerprint. Cross-ATS dedup (Level 3) never fires for any job inserted by the realtime monitor.
- `source_type` → All realtime-inserted jobs have NULL source_type. Auto-apply classification always returns "EXTERNAL" for these jobs.

**Fix required:** Add `fingerprint` (computed via `compute_fingerprint`) and `source_type` (from `r_job.get("source", "ats")`) to `new_rows` entries.

---

### P1-5 · `dedup.py` imports `fingerprint_job` from security but never uses it — dead import + duplicate logic
**File:** `app/services/dedup.py:9`  
**Severity:** DEAD CODE + maintenance hazard

```python
from app.core.security import fingerprint_job, hash_content   # fingerprint_job never called

def compute_fingerprint(...) -> str:   # identical logic to fingerprint_job in security.py
    raw = f"{...}::{company_id}::{location_key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]
```

`fingerprint_job` in `security.py` is an exact duplicate of `compute_fingerprint` in `dedup.py`. One must be the canonical source; the other must call it or be deleted.

**Fix required:** Remove `fingerprint_job` from `security.py`. Keep `compute_fingerprint` in `dedup.py` as canonical. Remove dead import.

---

### P1-6 · `ai_cost_control.py` spam gate uses fit-score setting as spam threshold — wrong semantic
**File:** `app/services/ai_cost_control.py:28`  
**Severity:** SILENT LOGIC BUG — wrong gate condition

```python
if job.get("spam_score", 0) >= settings.AI_MIN_FIT_SCORE_FOR_LLM / 100:
    return False, "spam_score_too_high"
# AI_MIN_FIT_SCORE_FOR_LLM = 60 → threshold = 0.60
```

`AI_MIN_FIT_SCORE_FOR_LLM` is a **fit score** minimum (on a 0–100 scale), not a spam threshold. Using it divided by 100 as a spam gate is semantically wrong. The gate fires when `spam_score >= 0.60`, which happens to be numerically acceptable — but adding a new config setting `AI_SPAM_GATE_THRESHOLD = 0.60` would fix the semantics without changing behavior.

**Fix required:** Add `AI_SPAM_GATE_THRESHOLD: float = 0.60` to `Settings` and use it directly.

---

### P1-7 · `self_corrector._check_source_signals` checks `feedback_notes` for "staffing" — correction never fires
**File:** `app/ai/agent/self_corrector.py` (~line 105)  
**Severity:** SILENT BUG — learning signal permanently dead

```python
for dec, fb in rows:
    if fb.feedback_notes and "staffing" in str(fb.feedback_notes).lower():
        staffing_negatives += 1
```

Source quality is tracked on `Job.source_type`, not in free-text `feedback_notes`. Users don't type "staffing" in their feedback notes. This correction never accumulates enough count to fire, so the "reduce staffing agency weight" memory is never written.

**Fix required:** Join to `Job` in the query and check `job.source_type == "STAFFING_AGENCY"` (or the ATS-name equivalent).

---

### P1-8 · `decision_agent.py` returns bare strings — violates `DecisionType` system
**File:** `app/ai/decision_agent.py` (throughout `make_decision()`)  
**Severity:** CONSISTENCY BUG — works due to `str` enum but fragile

`make_decision()` assigns and returns bare Python strings (`"APPLY_NOW"`, `"SKIP"`, etc.) rather than `DecisionType` members. Since `DecisionType` inherits from `str`, runtime comparisons work, but:
1. The `DecisionOutput.decision` field has no type constraint enforcing valid values.
2. A typo (e.g. `"APPLY NOW"`) would silently produce an invalid DB value.

**Files with bare-string decision usage:**
- `decision_agent.py` — 12 occurrences
- `evaluator.py` — 4 occurrences  
- `career_agent.py` — 2 occurrences
- `planner.py` — 1 occurrence
- `learning_engine.py` — 1 `.in_()` call
- `api/v1/jobs.py:161` — 1 query filter

**Fix required:** Import and use `DecisionType` in all these files. Add `str` type constraint to `DecisionOutput.decision`.

---

## 🟡 P2 — DATA CORRECTNESS ISSUES

### P2-1 · CareerAgent loads ALL users' jobs without role/location filter — O(n) memory + wrong decisions
**File:** `app/ai/agent/career_agent.py:60`

```python
q = await self.db.execute(
    select(Job).where(
        and_(Job.active == True, Job.first_seen_at >= cutoff,
             Job.role_category != "Not Relevant")   # ← no user filter
    ).limit(200)
)
```

Every user's CareerAgent loads the same 200 most-recent jobs regardless of the user's `target_roles` or `target_locations`. For a system with 100 users, all 100 agents load the same generic job list. User A who wants "Data Engineer in London" sees the same jobs as User B who wants "QA/SDET in Austin". This wastes 98% of scoring cycles and degrades decision quality.

**Fix required:** Filter by `Job.role_category.in_(user.target_roles)` and optionally by `Job.country.in_(user.target_locations)`.

---

### P2-2 · N+1 query pattern in `planner.generate_weekly_plan()`
**File:** `app/ai/agent/planner.py:74`

```python
for dec in top_decisions:   # 5 decisions
    result = await db.execute(select(Job).where(Job.id == dec.job_id))  # 5 queries
    job = result.scalar_one_or_none()
```

**Fix required:** Join `AIDecision` with `Job` in a single query, or use `select(Job).where(Job.id.in_([d.job_id for d in top_decisions]))`.

---

### P2-3 · `freshness.compute_freshness()` returns `"stale"` for both 3–14 days and >14 days — no expired label
**File:** `app/services/freshness.py`

```python
if age <= timedelta(days=14):
    return "stale"   # 3–14 days
return "stale"        # >14 days — identical label, no differentiation
```

Jobs 4 days old and 60 days old both get `freshness_label = "stale"`. The ranking engine scores both at `0.2`. Decision engine applies the same `-8` recency penalty to a 4-day-old job and a 60-day-old job. Old jobs should be marked `"expired"` and deprioritized or filtered out entirely.

**Fix required:** Add `"expired"` label for `age > timedelta(days=14)`.

---

### P2-4 · `memory_store.fit_adjustment` accumulates without bound — can permanently suppress decisions
**File:** `app/ai/agent/memory_store.py` + `self_corrector.py`

Each negative seniority memory writes `-5` to `fit_adjustment`. With 6 negative seniority outcomes, `fit_adjustment = -30`. A job scoring 80 becomes 50 — dropping from APPLY_NOW to SKIP. There's no cap.

**Fix required:** Clamp `fit_adjustment` in `[-15, +15]` in `get_adjustments()`.

---

### P2-5 · `ai_tasks._update_intelligence()` fires 2000+ queries for 1000 companies — performance bomb
**File:** `app/workers/ai_tasks.py:97`

```python
for company in companies:   # 1000 companies
    jobs_7d_q  = await db.execute(select(func.count(Job.id))...)  # query 1
    jobs_30d_q = await db.execute(select(func.count(Job.id))...)  # query 2
```

This runs 2 queries per company × 1000 companies = **2000 sequential DB queries** in a single Celery task. Easily reduceable to a single grouped aggregation query.

**Fix required:** Replace loop with single `GROUP BY company_id` + `CASE` aggregation.

---

### P2-6 · Missing skills format inconsistency between `decision_engine.py` and `decision_agent.py`
**File:** `app/ai/decision_engine.py` vs `app/ai/decision_agent.py`

`decision_engine.py` stores missing skills as formatted strings:
```python
insights["missing_skills"].append(f"{m.title()} (High severity) — Core requirement")
```

`decision_agent.py` stores them as plain skill names:
```python
missing_skills=match.missing_skills   # → ["Python", "Spark", "dbt"]
```

`learning_engine.py` and `planner.py` read `dec.missing_skills` and treat them as plain skill names. Skills stored by `decision_engine.py` will appear as `"Python (High Severity) — Core Requirement"` in skill gap analysis — never matching resume skills.

**Fix required:** Standardize `AIDecision.missing_skills` to always store plain skill name strings. Move severity/reason metadata elsewhere (e.g. `missing_skill_details` JSON field).

---

## 🟠 P3 — ARCHITECTURE / PERFORMANCE

### P3-1 · Three parallel ingestion pipelines — inconsistent data, maintenance nightmare

| Pipeline | Trigger | Bronze Layer | Dedup Method | source_type | fingerprint |
|---|---|---|---|---|---|
| `scan_tasks._scan_company_async()` | Celery Beat ✅ | ✅ BronzeRawJob | DedupEngine one-by-one ⚠ | source classifier ⚠ | ✅ computed |
| `realtime_monitor._fetch_delta()` | FastAPI lifespan ✅ | ❌ None | Bulk DO NOTHING ✅ | missing ⚠ | ❌ missing |
| `job_pipeline.process_company_jobs()` | APScheduler / tests | ❌ None | Bulk DO UPDATE ✅ | ATS name ✅ | ✅ computed |

Celery Beat triggers Path A (scan_tasks), which crashes on every run (P0-1). The scheduler triggers Path C, but the scheduler is a separate standalone process and not part of the standard deployment. Path B (realtime monitor) runs but inserts incomplete rows.

**Fix required:** Designate one canonical Silver-layer pipeline. Eliminate the others or clearly scope them (realtime-only vs full). Write `normalize_job()` to unblock Path A.

### P3-2 · Two independent fit-scoring systems produce different scores for the same job

| System | Entry point | Resume data source | Score range | Decision labels |
|---|---|---|---|---|
| `decision_engine.py` | API `/jobs/{id}/analyze` | `ResumeVersion` ORM | 0–100 pts | `DecisionType` ✅ |
| `decision_agent.py` + `resume_matcher.py` | `CareerAgent` | dict passed in | 0–100 % | bare strings ⚠ |

A job can receive different decisions from the two systems. The API shows one decision; the CareerAgent stores a different one. Users see inconsistency between API responses and agent-generated decisions.

### P3-3 · `DedupEngine.upsert_job()` in scan_tasks — O(n) per-row DB queries
**Fix required:** Replace with bulk ON CONFLICT DO UPDATE identical to `job_pipeline.py`.

### P3-4 · `career_agent._observe()` — Python-side dedup filter instead of SQL
```python
decided_ids = {row[0] for row in decided_ids_q.fetchall()}
new_jobs = [j for j in all_new_jobs if j.id not in decided_ids]
```
Loads up to 200 jobs then discards already-decided ones in Python. Should use SQL NOT EXISTS.

---

## 🟢 P4 — CLEANUP / MINOR

| ID | Location | Issue |
|----|----------|-------|
| P4-1 | `database.py` | `get_async_db()` and `get_db()` both exist and are nearly identical. `get_async_db` skips rollback on exception. Use one. |
| P4-2 | `dedup.py` | `hash_content` imported from `security.py` but unused in this file |
| P4-3 | `freshness.py` | Repost detection code path (lines checking `last_seen_at`) is unreachable — never reaches a return |
| P4-4 | `career_agent.py` | `db.commit()` called after loop body, then `_learn()` calls `db.commit()` again |
| P4-5 | `decision_engine.py` | `user_stats["top_scores_avg"]` hardcoded to `75.0` — never computed from real data |
| P4-6 | `evaluator.py` | `interview_rate = positive_outcomes / max(apply_now_count, 1)` — should divide by applied-count, not APPLY_NOW-count |
| P4-7 | `career_agent.py` | `_run_stats["apply_now"]` uses bare string `"APPLY_NOW"` comparison |
| P4-8 | `alembic/001` | `job_embeddings` and `resume_embeddings` tables missing PK autoincrement definition |
| P4-9 | `config.py` | `SCAN_MAX_RETRY_DELAY = 60.0` is seconds but `backoff_mins` in scan_tasks divides by 60 — unit inconsistency in comments |
| P4-10 | `models/ai_models.py` | `DecisionType` enum defined at top of file, then imports happen below — unusual ordering, no functional issue |

---

## Schema vs ORM Mismatch Summary

| Column | ORM Model | Alembic Migration |
|--------|-----------|-------------------|
| `jobs.city` | ✅ String(100), index | ❌ MISSING |
| `jobs.region` | ✅ String(100), index | ❌ MISSING |
| `jobs.normalized_location` | ✅ String(500), index | ✅ present (no separate index entry) |
| `job_embeddings.embedding` | ✅ Vector/JSON conditional | ❌ Added via raw SQL `ALTER TABLE` only |
| `resume_embeddings.embedding` | ✅ Vector/JSON conditional | ❌ Added via raw SQL `ALTER TABLE` only |

---

## Architecture Diagram (Current State)

```
┌─────────────────────────────────────────────────────────────────────┐
│                         INGESTION LAYER                             │
│                                                                     │
│  Celery Beat ──► scan_tier_companies                                │
│                      └──► run_company_scan_task ──► _scan_company_async()  │
│                               │                                     │
│                               │  ⛔ CRASHES: normalize_job missing  │
│                               ▼                                     │
│                         [DEAD PATH]                                 │
│                                                                     │
│  FastAPI lifespan ──► realtime_monitor (every 50s)                  │
│                           └──► _fetch_delta() ──► ON CONFLICT DO NOTHING │
│                                    ⚠ Missing fingerprint, source_type    │
│                                                                     │
│  APScheduler/tests ──► run_ingestion_pipeline()                     │
│                           └──► process_company_jobs() ──► bulk upsert    │
│                                    ✅ Best implementation                 │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          jobs table                                 │
│  fingerprint(nullable) | source_type(inconsistent) | city(missing) │
└─────────────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
        ┌───────────────────┐  ┌────────────────────┐
        │   CareerAgent     │  │  decision_engine   │
        │  (Celery every    │  │  (API per-request) │
        │   15 min)         │  │                    │
        │  resume_matcher   │  │  calculate_fit_    │
        │  + decision_agent │  │  score()           │
        │  [bare strings]⚠  │  │  [DecisionType]✅  │
        │  no user filter⚠  │  │                    │
        └─────────┬─────────┘  └────────┬───────────┘
                  │                     │
                  └──────────┬──────────┘
                             ▼
                    ┌────────────────┐
                    │  ai_decisions  │
                    │  (mixed format)│
                    └────────────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
     learning_engine   self_corrector   auto_apply
     [skill gap]       [source check⚠] [rate-limited]
```

---

## Issue Count Summary

| Priority | Count | Impact |
|----------|-------|--------|
| P0 — Runtime Crash | 1 | Every Celery scan fails silently |
| P0 — Type annotation | 1 | Breaks tooling |
| P1 — Silent Logic Bug | 8 | Wrong data in DB, wrong AI decisions |
| P2 — Data Correctness | 6 | Degraded quality, subtle bugs |
| P3 — Architecture | 4 | Scale failure, maintenance cost |
| P4 — Cleanup | 10 | Minor quality debt |
| **Total** | **30** | |

---

## Recommended Fix Sequence (Phases 2–10)

| Phase | Focus | Files Affected | Risk |
|-------|-------|----------------|------|
| **2** | Write `normalize_job()`, fix `_analyze_job` annotation, fix P0-1 | `normalizer.py`, `career_agent.py` | LOW — additive |
| **3** | Fix `city`/`region` migration, fix `source_type` standardization | `alembic/001`, `scan_tasks.py` | MEDIUM — schema |
| **4** | Fix `realtime_monitor` missing fields, fix `freshness` expired label | `realtime_monitor.py`, `freshness.py` | LOW — additive |
| **5** | Unify `DecisionType` across all callers | 6 files | LOW — mechanical |
| **6** | Fix `success_rate` formula, fix `self_corrector` source check | `decision_engine.py`, `self_corrector.py` | LOW |
| **7** | Fix `missing_skills` format inconsistency | `decision_engine.py`, `learning_engine.py` | MEDIUM |
| **8** | Fix `career_agent._observe()` user filter + N+1s | `career_agent.py`, `planner.py` | MEDIUM |
| **9** | Fix `ai_tasks` company intelligence 2000-query loop | `ai_tasks.py` | LOW |
| **10** | Cleanup: dead imports, duplicate logic, minor fixes | multiple | LOW |

---

## Assumptions Made

1. `normalize_job()` is intended to map a `RawJob` dataclass to the `job_data` dict schema used in `_scan_company_async`. The implementation must be inferred from the dict keys accessed after the call.
2. `source_type` on `Job` is intended to store ATS provider name ("greenhouse", "lever"), not source classifier category ("DIRECT_COMPANY"). The API's auto-apply classification confirms this.
3. The canonical pipeline going forward should be `scan_tasks.py` (Celery-triggered, has Bronze audit trail), fixed to use bulk upsert.
4. The zero-vector embedding stub is acceptable for Phase 1. Real embeddings are Phase 2 work.

---

## Remaining Risks After All Fixes

1. **Embedding similarity is meaningless until real embeddings are generated.** All jobs have identical zero vectors. Vector search returns arbitrary results.
2. **Two scoring systems (decision_engine vs resume_matcher) will still produce different scores** for the same job. Full unification is a larger refactor.
3. **`robots.txt` compliance is a stub** — `check_robots_txt` always returns `True`. If a company explicitly disallows scraping, it will not be respected.
4. **Rate limiter** (`RateLimiter` in `services/rate_limiter.py`) behavior was not audited — not read in this pass.
5. **No test coverage** — fixes cannot be regression-tested without a test suite.

---

*Audit complete. Ready for Phase 2 implementation.*
