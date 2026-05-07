# JobJarvis — Full System Audit Report
**Auditor:** Principal Staff Engineer / AI Systems Architect  
**Date:** 2026-05-04  
**Scope:** Full backend codebase — models, services, workers, AI layer, connectors, pipeline  
**Verdict:** System has a functional skeleton but contains **6 runtime-crash bugs**, **8 severe logic errors**, and **12+ incomplete features** that prevent true autonomous operation.

---

## Table of Contents
1. [System Architecture Map](#1-system-architecture-map)
2. [Data Flow Diagram](#2-data-flow-diagram)
3. [Critical Issues — P0 (Runtime Crashes)](#3-critical-issues--p0-runtime-crashes)
4. [High Severity — P1 (Severe Logic Bugs)](#4-high-severity--p1-severe-logic-bugs)
5. [Medium Severity — P2 (Missing / Incomplete)](#5-medium-severity--p2-missing--incomplete)
6. [Low Severity — P3 (Code Quality / Debt)](#6-low-severity--p3-code-quality--debt)
7. [Missing Index Analysis](#7-missing-index-analysis)
8. [Dead Code Inventory](#8-dead-code-inventory)
9. [Phased Fix Plan](#9-phased-fix-plan)
10. [Risk & Tradeoff Summary](#10-risk--tradeoff-summary)

---

## 1. System Architecture Map

```
┌─────────────────────────────────────────────────────────────────┐
│                        ENTRY POINTS                              │
│   FastAPI (main.py)     Celery Beat     APScheduler (scheduler) │
│        │                    │                    │               │
│        │           ┌────────┴────────┐           │               │
│        │           │  scan_tasks.py  │           │               │
│        │           │  ai_tasks.py    │           │               │
│        │           └────────┬────────┘           │               │
│        │                    │                    │               │
│        ▼                    ▼                    ▼               │
│   API Routers        Celery Workers      job_pipeline.py         │
│   (v1/*.py)          (async inside       run_ingestion_pipeline  │
│        │              sync tasks)              │                 │
│        │                    │    ← OVERLAP ────►│                │
└────────┼────────────────────┼───────────────────┼───────────────┘
         │                    │                   │
         ▼                    ▼                   ▼
    ┌──────────┐     ┌──────────────┐    ┌──────────────┐
    │  FastAPI │     │ _scan_company│    │ process_     │
    │  depends │     │ _async()     │    │ company_jobs │
    │  (get_db)│     │ Bronze→Gold  │    │ simplified   │
    └────┬─────┘     └──────┬───────┘    └──────┬───────┘
         │                  │                   │
         ▼                  ▼                   ▼
    ┌─────────────────────────────────────────────┐
    │              SQLAlchemy 2.0 Async            │
    │         PostgreSQL 16 + pgvector             │
    │   (dev: SQLite via aiosqlite)                │
    └─────────────────────────────────────────────┘

AI Layer:
  CareerAgent → [role_classifier, skill_extractor, spam_detector,
                  work_auth_detector, source_classifier, resume_matcher,
                  decision_agent] → AIDecision → MemoryStore → SelfCorrector

Connectors:
  GreenhouseConnector │ LeverConnector │ AshbyConnector │
  SmartRecruitersConnector │ WorkdayConnector │ ICIMSConnector
  All inherit BaseConnector (retry + rate limit + jitter)

ALSO: ats.py (standalone fetch functions, used by job_pipeline.py)
  ← PARALLEL implementation to the class-based connectors above
```

---

## 2. Data Flow Diagram

```
ATS / Aggregator
      │
      ▼
[Fetch] → raw_jobs (list of dicts or RawJob objects)
      │
      ├── Via job_pipeline.py (simple):
      │     normalize → spam_gate → quality_gate → bulk_upsert (ON CONFLICT)
      │     ⚠ No fingerprint set │ No source_type set │ No BronzeRawJob saved
      │
      └── Via scan_tasks.py (full Bronze→Silver→Gold):
            BronzeRawJob saved → normalize_job() → compute_fingerprint()
            → dedup.upsert_job() → AIDecision (via CareerAgent)

Dedup:
  Level 1: company_id + external_id  (ON CONFLICT in bulk upsert)
  Level 2: job_url                   (unique constraint)
  Level 3: fingerprint               (⚠ NULL in simple pipeline)

Decision:
  CareerAgent → resume_matcher → decision_agent → AIDecision
  (labels: APPLY_NOW | TAILOR_RESUME_FIRST | SAVE_FOR_LATER | SKIP | HIGH_RISK | REVIEW_NEEDED)

  evaluate_job_decision() [API endpoint] → decision_engine.py
  (labels: APPLY | REVIEW | SKIP)   ← ⚠ DIFFERENT LABEL SET

Learning:
  learning_engine.compute_skill_gaps() queries:
    AIDecision.decision.in_(["APPLY_NOW", "TAILOR_RESUME_FIRST", "SAVE_FOR_LATER"])
  ← ⚠ WILL MATCH ZERO ROWS if jobs were scored via evaluate_job_decision()
```

---

## 3. Critical Issues — P0 (Runtime Crashes)

### P0-1 — `process_company_jobs` Return Type Mismatch → ValueError at Runtime

**File:** `app/services/job_pipeline.py`  
**Lines:** 408, 494

The function is typed as `-> tuple[int, int] | None` and the caller destructures:
```python
fetched, attempted = res
```
But the function returns `metrics` — a **dict with 6 keys**, not a tuple of 2.

Python's destructuring of a dict iterates the **keys**, not values. With 6 keys this raises:
```
ValueError: too many values to unpack (expected 2)
```
This crashes **every call** to `run_ingestion_pipeline()`. The simple ingestion pipeline is **completely broken**.

**Fix:**
```python
# In process_company_jobs, change the final return from:
return metrics
# To:
return metrics["fetched"], metrics["inserted"]

# OR update the caller to handle the full metrics dict:
res = await process_company_jobs(...)
if isinstance(res, dict):
    metrics["total_jobs_fetched"]  += res.get("fetched", 0)
    metrics["total_jobs_inserted"] += res.get("inserted", 0)
```

---

### P0-2 — `asyncio.get_event_loop()` in Celery Workers → RuntimeError on Python 3.10+

**Files:** `app/workers/scan_tasks.py:16`, `app/workers/ai_tasks.py:10`

```python
def _run_async(coro):
    return asyncio.get_event_loop().run_until_complete(coro)
```

Celery workers run on background **threads**, not the main thread. In Python 3.10+, `get_event_loop()` on a background thread with no running loop logs a `DeprecationWarning`. In Python **3.12+** (which will be the next LTS), it raises a `RuntimeError`. This means **all Celery scan and AI tasks are broken** on Python 3.12+.

**Fix:**
```python
def _run_async(coro):
    # Python 3.10+ safe: creates a new event loop for this thread
    return asyncio.run(coro)
```

---

### P0-3 — Dual Decision Label System: Learning Engine Queries Return Zero Rows

**Files:** `app/ai/decision_engine.py`, `app/ai/learning_engine.py`, `app/ai/decision_agent.py`

There are **two decision engines with incompatible label sets**:

| Engine | Labels Produced | Used By |
|--------|----------------|---------|
| `decision_engine.evaluate_job_decision()` | `"APPLY"`, `"REVIEW"`, `"SKIP"` | API endpoint `/jobs/{id}/analyze` |
| `decision_agent.make_decision()` | `"APPLY_NOW"`, `"TAILOR_RESUME_FIRST"`, `"SAVE_FOR_LATER"`, `"SKIP"`, `"HIGH_RISK"`, `"REVIEW_NEEDED"` | CareerAgent loop |

`learning_engine.compute_skill_gaps()` queries:
```python
AIDecision.decision.in_(["APPLY_NOW", "TAILOR_RESUME_FIRST", "SAVE_FOR_LATER"])
```
Any decision written by `evaluate_job_decision()` (via the API) will **never appear in this query**. The learning loop silently processes zero records for API-driven decisions. The `ai_models.py` docstring lists the APPLY_NOW vocabulary, but the DB will contain a mix of both label sets.

**Fix:** Standardize on one label set. Canonical set should be:
`APPLY_NOW | TAILOR_RESUME_FIRST | SAVE_FOR_LATER | SKIP | HIGH_RISK | REVIEW_NEEDED`

Update `decision_engine.py` to map its internal categories to this set:
```python
# In evaluate_job_decision():
if score >= threshold_apply:
    decision_type = "APPLY_NOW"
elif score >= threshold_review:
    decision_type = "TAILOR_RESUME_FIRST"
else:
    decision_type = "SKIP"
```

---

### P0-4 — `fingerprint` Never Set in `job_pipeline.py` → Fingerprint-Based Dedup Broken

**File:** `app/services/job_pipeline.py`  
**Line:** ~310 (jobs_to_insert.append block)

`compute_fingerprint` is imported at the top of the file but **never called**. The `jobs_to_insert` dict omits the `fingerprint` key entirely. All jobs inserted via the simple pipeline will have `fingerprint = NULL`.

The `DedupEngine.find_duplicate()` uses fingerprint as an OR condition:
```python
conditions = [Job.fingerprint == fingerprint]
```
With `NULL == NULL` evaluating to `NULL` in SQL (not TRUE), the fingerprint level of dedup **never fires**. The third dedup level is dead for ~100% of the data.

**Fix:**
```python
from app.services.dedup import compute_fingerprint

# Inside the enrichment loop, after normalize_location:
fp = compute_fingerprint(title.lower(), company.id, location.lower())

# Add to jobs_to_insert:
jobs_to_insert.append({
    ...
    "fingerprint": fp,
    "source_type": r_job.get("source", "ats"),  # also missing — see P1-4
    ...
})
```

---

### P0-5 — `JobEmbedding` and `ResumeEmbedding` ORM Models Missing Vector Column

**File:** `app/models/ai_models.py`

The `JobEmbedding` and `ResumeEmbedding` models exist but the actual `embedding` column (pgvector type) is only added via **raw SQL in the migration**:
```sql
ALTER TABLE job_embeddings ADD COLUMN IF NOT EXISTS embedding vector(1536)
```
The ORM model has no `embedding` mapped column. This means:
- You cannot query `JobEmbedding.embedding` via SQLAlchemy
- Any ORM code that tries to access `.embedding` will raise `AttributeError`
- The entire semantic search layer is inaccessible via the ORM

**Fix:**
```python
# In ai_models.py, after the pgvector import guard:
if _VECTOR_AVAILABLE:
    from pgvector.sqlalchemy import Vector

class JobEmbedding(Base):
    __tablename__ = "job_embeddings"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, unique=True)
    model: Mapped[str] = mapped_column(String(100), default="text-embedding-3-small")
    # Add this:
    embedding: Mapped[Optional[list]] = mapped_column(
        Vector(settings.VECTOR_DIMENSIONS) if _VECTOR_AVAILABLE else JSON,
        nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

---

### P0-6 — No Embedding Generation Code Exists Anywhere

**Scope:** Entire codebase

Searched all `.py` files for: `generate_embedding`, `create_embedding`, `embed_job`, `openai.*embed`, `client.embeddings.create`. **Zero results.**

The `job_embeddings` and `resume_embeddings` tables exist. The alembic migration creates the ivfflat index. The `VECTOR_DIMENSIONS` setting is defined. But there is **no code that calls the OpenAI embeddings API or any other embedding model**. Phase 5 (the entire semantic matching layer) is pure scaffolding with no implementation. The system will never generate a single embedding.

**Needs:** A new `app/services/embedding_service.py` module (see Phase 5 plan).

---

## 4. High Severity — P1 (Severe Logic Bugs)

### P1-1 — `user_stats["success_rate"]` Always Equals 0.2

**File:** `app/ai/decision_engine.py:448`

```python
user_stats = {
    "success_rate": total_interviews / max(1, total_interviews * 5),
    # = total_interviews / (total_interviews * 5) = 1/5 = 0.2 ALWAYS
}
```

The denominator is `total_interviews * 5` — a fixed multiple of the numerator. This evaluates to exactly `0.2` whenever `total_interviews > 0`, and `0.0` when there are no interviews. The variable is checked with `if user_stats.get("success_rate", 0) > 0.1` — so it's always `True` when any interviews exist, adding a fixed `prob *= 1.2` boost regardless of actual performance.

**Fix:**
```python
# Need total applications, not total * 5
total_apps_q = await db.execute(
    select(func.count(AIDecisionFeedback.id))
    .select_from(AIDecisionFeedback)
    .join(AIDecision)
    .where(AIDecision.user_id == user.id)
)
total_apps = total_apps_q.scalar() or 0

user_stats = {
    "success_rate": total_interviews / max(1, total_apps),
}
```

---

### P1-2 — N+1 Query in `_update_intelligence()`: 3 Queries × 1000 Companies

**File:** `app/workers/ai_tasks.py:86-118`

```python
for company in companies:  # 1000 companies
    jobs_7d_q  = await db.execute(...)  # query 1
    jobs_30d_q = await db.execute(...)  # query 2
    intel_q    = await db.execute(...)  # query 3
# Total: 3,000 sequential DB round-trips per run
```

This scales linearly and will become the dominant cost as the company list grows.

**Fix — replace with 3 bulk aggregation queries:**
```python
from sqlalchemy import case

# Single query for all companies
jobs_counts = await db.execute(
    select(
        Job.company_id,
        func.count(case((Job.first_seen_at >= week_ago, 1))).label("jobs_7d"),
        func.count(case((Job.first_seen_at >= month_ago, 1))).label("jobs_30d"),
    )
    .where(Job.company_id.in_([c.id for c in companies]))
    .group_by(Job.company_id)
)
counts_by_company = {row.company_id: row for row in jobs_counts}

# Bulk load existing intelligence rows
intel_map = {i.company_id: i for i in (await db.execute(
    select(CompanyIntelligence)
    .where(CompanyIntelligence.company_id.in_([c.id for c in companies]))
)).scalars().all()}

# Then iterate in Python, no more DB hits in loop
```

---

### P1-3 — CORS Configuration Uses Hardcoded List, Ignores `settings.CORS_ORIGINS`

**File:** `app/main.py:128`

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000"
    ],  # ← hardcoded, ignores settings.CORS_ORIGINS
    ...
)
```

`settings.CORS_ORIGINS` in `config.py` includes `localhost:8000` and `127.0.0.1:8000` as well, but the middleware configuration doesn't use it. In production, adding an allowed origin requires a code deploy, not a config change.

**Fix:**
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

---

### P1-4 — `source_type` Not Set in `job_pipeline.py` → Scoring Signal Lost

**File:** `app/services/job_pipeline.py` (jobs_to_insert block)

The Job model has both a `source` field (ATS name string) and a `source_type` field (classified category: `direct|ats|aggregator|staffing`). The simple pipeline sets `"source": r_job.get("source")` but never sets `"source_type"`. This means:
- `decision_engine.py` logic that checks `job.source_type` defaults to `"unknown"`
- Company signal score never awards the +5 pts for `source_confidence >= 0.9`
- Source quality classification is silently dropped

**Fix:** Add `"source_type": r_job.get("source", "ats")` to the `jobs_to_insert` dict.

---

### P1-5 — `CareerAgent._observe()` Loads ALL Active Jobs Without User Preference Filter

**File:** `app/ai/agent/career_agent.py:53-68`

```python
q = await self.db.execute(
    select(Job).where(
        and_(
            Job.active == True,
            Job.first_seen_at >= cutoff,  # last 24h
            Job.role_category != "Not Relevant",
        )
    ).order_by(Job.first_seen_at.desc()).limit(200)
)
```

At 50k companies with hourly scans, this query will return 200 jobs **from any role, any location, any company** for every user. The user's `target_roles`, `target_locations`, and `work_authorization` are not applied at the DB level — only spam/role filtering happens. This means:
- A user targeting "ML Engineer" jobs processes 200 random jobs each hour
- The `decided_ids` in-memory filter still requires loading all 200 rows
- At scale, user agents will generate thousands of irrelevant decisions

**Fix:**
```python
user_q = await self.db.execute(select(User).where(User.id == self.user_id))
user = user_q.scalar_one_or_none()

conditions = [
    Job.active == True,
    Job.first_seen_at >= cutoff,
    Job.role_category != "Not Relevant",
    ~Job.id.in_(decided_ids),  # exclude already-decided inline
]
if user and user.target_roles:
    conditions.append(Job.role_category.in_(user.target_roles))
if user and not user.open_to_remote:
    conditions.append(Job.remote_type != "remote")
```

---

### P1-6 — Company Permanently Deactivated After 3 Consecutive Failures (No Reactivation)

**File:** `app/services/job_pipeline.py:209-213`

```python
if company.consecutive_failures >= 3:
    company.active = False  # ← permanent, no reactivation path
```

Transient network outages (DNS flap, ATS rate-limit, SSL renewal) will permanently deactivate legitimate companies. The system has no reactivation mechanism — no scheduled job to probe inactive companies, no admin endpoint to re-enable by domain pattern.

**Fix:** Instead of hard deactivation, implement exponential backoff with a max-cooldown:
```python
if company.consecutive_failures >= 3:
    # Backoff: pause for up to 7 days, then try again automatically
    backoff_hours = min(168, 2 ** company.consecutive_failures)
    company.next_scan_at = run_now + timedelta(hours=backoff_hours)
    logger.warning(
        "company_backoff company=%s failures=%d next_retry=%s",
        company.name, company.consecutive_failures,
        company.next_scan_at.isoformat()
    )
    # Only deactivate after extended failures (e.g. 10+ consecutive)
    if company.consecutive_failures >= 10:
        company.active = False
```

---

### P1-7 — `_check_source_signals` Checks Free-Text Notes for "staffing" String

**File:** `app/ai/agent/self_corrector.py:108-114`

```python
if fb.feedback_notes and "staffing" in str(fb.feedback_notes).lower():
    staffing_negatives += 1
```

Users will essentially never type "staffing" in freeform feedback notes. This correction will never fire. The correct approach is to join to the Job table and check `Job.source_type`.

**Fix:**
```python
from app.models.job import Job

q = await self.db.execute(
    select(AIDecision, AIDecisionFeedback, Job)
    .join(AIDecisionFeedback, AIDecisionFeedback.ai_decision_id == AIDecision.id)
    .join(Job, Job.id == AIDecision.job_id)
    .where(
        and_(
            AIDecision.user_id == self.user_id,
            AIDecisionFeedback.outcome == "negative",
            Job.source_type.in_(["staffing", "aggregator"]),
        )
    )
)
```

---

### P1-8 — Dual Pipeline Architecture Creates Race Conditions and Audit Gaps

**Files:** `app/services/job_pipeline.py`, `app/workers/scan_tasks.py`, `app/services/scheduler.py`

There are **two completely separate pipeline implementations** for the same task:

| | `job_pipeline.py` | `scan_tasks.py` |
|--|--|--|
| Bronze layer | ❌ Not saved | ✅ `BronzeRawJob` created |
| Fingerprint | ❌ Not computed | ✅ `compute_fingerprint()` called |
| `source_type` | ❌ Not set | ✅ `classify_source()` called |
| `ScanRun` record | ❌ Not created | ✅ Created |
| Work auth detection | ❌ Not called | ✅ `detect_work_auth()` called |
| Triggered by | APScheduler + realtime_monitor | Celery Beat |

Both are scheduled to run for the same companies. `scheduler.py` calls `run_ingestion_pipeline()` on the same cadence as Celery Beat calls `scan_tier_companies`. If both are running simultaneously, the same company can be processed twice — creating duplicate `BronzeRawJob` rows and redundant upserts.

**Recommendation:** Eliminate `job_pipeline.py` as the primary ingestion path. Make Celery Beat / `scan_tasks.py` the single canonical pipeline. Keep `job_pipeline.py` only as a development/fallback tool clearly marked as such.

---

## 5. Medium Severity — P2 (Missing / Incomplete)

### P2-1 — Realtime Monitor Watermarks Are In-Memory Only

**File:** `app/services/realtime_monitor.py`

The `_watermark` dict is a module-level dict in-process. Every FastAPI restart resets all watermarks to empty. For the next full tick, every Tier-1 company is treated as if all its jobs are new — triggering bulk re-inserts that hit ON CONFLICT on every row. At scale (hundreds of Tier-1 companies, thousands of jobs), this creates a write thunderstorm on every restart.

**Fix:** Persist watermarks to Redis with TTL:
```python
import redis.asyncio as aioredis

async def _get_watermark(company_id: int) -> set[str]:
    r = aioredis.from_url(settings.REDIS_URL)
    raw = await r.smembers(f"watermark:{company_id}")
    return {v.decode() for v in raw}

async def _update_watermark(company_id: int, external_ids: set[str]):
    r = aioredis.from_url(settings.REDIS_URL)
    if external_ids:
        await r.sadd(f"watermark:{company_id}", *external_ids)
        await r.expire(f"watermark:{company_id}", 86400)  # 24h TTL
```

---

### P2-2 — Salary Extraction Non-Existent

**Files:** `job_pipeline.py`, `scan_tasks.py`

`salary_min`, `salary_max`, `salary_currency`, `salary_period` are defined on the Job model. `scan_tasks.py` copies them from `normalize_job()` output. But `normalize_job()` in `normalizer.py` does not parse salary from description text. `job_pipeline.py` never sets these fields at all.

The compensation match in `decision_engine.py` falls back to `0.5` (neutral) when salary is unknown. This means the compensation signal in fit scoring is always neutral, even when salary is clearly stated in descriptions like "$150k-180k/year".

**Needs:** A `extract_salary(description: str)` function using regex patterns for common formats:
- `$\d{2,3}k`, `$\d{3},\d{3}`, `\d{2,3},\d{3} USD`, `£\d{2,3}k`, etc.

---

### P2-3 — Freshness Labels Don't Differentiate "Old" from "Very Old"

**File:** `app/services/freshness.py`

```python
for label, threshold in FRESHNESS_LABELS.items():
    if age <= threshold:
        return label

# Falls through to:
return "stale"  # covers both day-4 jobs AND year-old jobs
```

Any job older than 3 days gets `"stale"`. The `decision_engine.py` recency scoring correctly uses `days_old` counts, but `CostController.should_call_llm()` skips jobs with `freshness_label == "stale"` — which means **any job older than 3 days** is excluded from AI processing, including week-old jobs that are still very much worth analyzing.

**Fix:** Add granular labels:
```python
FRESHNESS_LABELS = {
    "new_last_hour":    timedelta(hours=1),
    "new_last_6_hours": timedelta(hours=6),
    "new_today":        timedelta(hours=24),
    "new_last_3_days":  timedelta(days=3),
    "active_week":      timedelta(days=7),   # ADD
    "active_2_weeks":   timedelta(days=14),  # ADD
    "stale":            timedelta(days=30),  # covers 14-30 days
    # >30 days → "expired"                  # ADD
}
```
Update `CostController` to allow `active_week` and `active_2_weeks` through.

---

### P2-4 — APScheduler and Celery Beat Both Scheduled for Same Cadence

**Files:** `app/services/scheduler.py`, `app/workers/celery_app.py`

Celery Beat schedules `scan_tier_companies` every hour for Tier-1. APScheduler in `scheduler.py` also runs `run_ingestion_pipeline()` (which processes all tiers). If both are deployed, every company gets processed **twice per cycle** with no coordination or locking.

**Fix:** Pick one. For production scale, Celery Beat is the right choice (distributed, recoverable). Deprecate APScheduler. For development, add a mutex:
```python
# In run_ingestion_pipeline():
lock_key = "pipeline_lock"
acquired = await redis_client.set(lock_key, "1", nx=True, ex=600)
if not acquired:
    logger.info("pipeline_already_running_skipping")
    return
```

---

### P2-5 — `compute_freshness()` Accepts `last_seen_at` But Ignores It

**File:** `app/services/freshness.py:14`

```python
def compute_freshness(first_seen_at, last_seen_at=None) -> str:
    # last_seen_at is accepted but never used in the function body
```

The comment in the code says "Detect repost: job disappeared and came back" — but there's no repost detection logic. Jobs that were previously closed and reposted should get a `"repost"` label for special handling.

---

### P2-6 — `AIDecision.created_at` Has No Database Index

**File:** `app/models/ai_models.py`

`SelfCorrector._check_seniority_corrections()` and `_check_skill_signals()` both filter:
```python
AIDecision.created_at >= datetime.now(timezone.utc) - timedelta(days=30)
```
There is no index on `ai_decisions.created_at`. With a growing decisions table this is a **full sequential scan** on every self-correction run (hourly for all users).

**Fix:** Add to `AIDecision.__table_args__`:
```python
Index("ix_decision_created_at", "created_at"),
Index("ix_decision_user_created", "user_id", "created_at"),
```

---

### P2-7 — `PROVIDER_STATE` Circuit Breaker Is Not Process-Safe

**File:** `app/services/job_pipeline.py:87`

```python
PROVIDER_STATE: dict = {}  # module-level, in-process only
```

With multiple Celery workers, each worker process has its own `PROVIDER_STATE`. A provider that's failing will trigger the circuit breaker in Worker A but Workers B, C, D will continue hammering it. The circuit breaker provides no actual protection at scale.

**Fix:** Store circuit breaker state in Redis:
```python
async def _is_circuit_open(ats_type: str) -> bool:
    r = aioredis.from_url(settings.REDIS_URL)
    return bool(await r.get(f"circuit:{ats_type}:open"))

async def _record_failure(ats_type: str):
    r = aioredis.from_url(settings.REDIS_URL)
    key = f"circuit:{ats_type}:fails"
    count = await r.incr(key)
    await r.expire(key, 600)
    if count >= 3:
        await r.set(f"circuit:{ats_type}:open", "1", ex=600)
```

---

### P2-8 — `evaluate_job_decision()` Commits Inside Single-Decision Flow

**File:** `app/ai/decision_engine.py` (near end of function)

```python
db.add(decision)
await db.commit()   # ← commit after every single decision
await db.refresh(decision)
```

When the API endpoint triggers batch analysis (e.g. `/jobs/analyze-all`), each call is a separate commit, causing high commit overhead. Should use `flush()` within a transaction and commit at the batch boundary.

---

## 6. Low Severity — P3 (Code Quality / Technical Debt)

### P3-1 — Late Import Inside `evaluate_job_decision()` (Performance)

**File:** `app/ai/decision_engine.py`

```python
from app.models.ai_models import AIDecisionFeedback  # inside the function body
```

This re-executes a module import on every call. While Python caches imports, the lookup still traverses `sys.modules` unnecessarily. Move to the top of the file.

---

### P3-2 — `BaseConnector.fetch()` Applies Jitter Before First Attempt

**File:** `app/connectors/base.py`

```python
async for attempt in AsyncRetrying(...):
    with attempt:
        jitter = random.uniform(0, settings.SCAN_JITTER_MAX)
        await asyncio.sleep(jitter)  # ← fires on attempt 1 too!
```

With `SCAN_JITTER_MAX = 3.0`, the very first request waits 0–3 seconds for no reason. Jitter should only apply to retries.

**Fix:**
```python
with attempt:
    if attempt.retry_state.attempt_number > 1:
        jitter = random.uniform(0, settings.SCAN_JITTER_MAX)
        await asyncio.sleep(jitter)
```

---

### P3-3 — `_run_agent_all()` Spawns N Tasks With No Concurrency Limit

**File:** `app/workers/ai_tasks.py:43-48`

```python
for user in users:
    run_career_agent_for_user.delay(user.id)  # fires unlimited tasks
```

With 1000 users, this dispatches 1000 Celery tasks simultaneously, saturating the queue. No batching, no rate limit.

**Fix:** Use Celery chord/group with concurrency limits or chunk:
```python
from celery import chunks
run_career_agent_for_user.chunks([(u.id,) for u in users], 10).apply_async()
```

---

### P3-4 — `check_robots_txt` in `compliance.py` Potentially Blocks Per-Job

Every call to `_scan_company_async()` calls `await check_robots_txt(domain)`. If this makes an HTTP request, it's a full round-trip per company per scan. This should be cached in Redis with a 24-hour TTL.

---

### P3-5 — `SENIORITY_COMPATIBILITY` Matrix Is Asymmetric Without Justification

**File:** `app/ai/resume_matcher.py:20`

```python
"mid": {"senior": 0.5},  # mid-level person applying to senior = 50%
"senior": {"mid": 0.7},  # senior person applying to mid = 70%
```

A senior applying to a mid-level role should not score higher than a mid-level applying to a senior role. The asymmetry is unexplained and may produce counterintuitive results (senior gets better score on a mid job than the matching mid-level candidate).

---

## 7. Missing Index Analysis

| Table | Column(s) | Query Pattern | Impact |
|-------|-----------|---------------|--------|
| `ai_decisions` | `created_at` | SelfCorrector 30-day lookups | Full scan on large table |
| `ai_decisions` | `(user_id, created_at)` | SelfCorrector per-user | Combined scan |
| `ai_memory` | `(user_id, memory_type)` | MemoryStore.get_memories() | Composite needed |
| `bronze_raw_jobs` | `ingested_at` | Unprocessed bronze queries | Full scan |
| `bronze_raw_jobs` | `(company_id, processed)` | Processing queue queries | Composite needed |
| `fetch_audit_logs` | `fetched_at` | Audit queries by date | Full scan |
| `application_answers` | `(user_id, question_type)` | Answer bank lookups | Missing composite |
| `scan_runs` | `started_at` | Recent scan queries | Full scan |

**Migration needed:**
```sql
CREATE INDEX ix_decision_user_created ON ai_decisions(user_id, created_at DESC);
CREATE INDEX ix_memory_user_type ON ai_memory(user_id, memory_type) WHERE is_active = true;
CREATE INDEX ix_bronze_company_unprocessed ON bronze_raw_jobs(company_id, processed) WHERE processed = false;
CREATE INDEX ix_fetch_audit_date ON fetch_audit_logs(fetched_at DESC);
CREATE INDEX ix_app_answers_user_type ON application_answers(user_id, question_type);
CREATE INDEX ix_scan_runs_started ON scan_runs(started_at DESC);
```

---

## 8. Dead Code Inventory

| Module | Status | Evidence |
|--------|--------|----------|
| `app/services/ranking.py` | Likely dead — not imported anywhere found | No references in workers/API |
| `app/services/market_trends.py` | Partially dead — only `update_company_intelligence` calls it indirectly | Check for direct callers |
| `app/services/application_copilot.py` | Not referenced in any route | No router import |
| `app/services/company_expander.py` | Not called by scheduler or worker | Standalone utility |
| `app/services/compliance.py` (robots check) | Called but result may be cached incorrectly | Verify per-scan cost |
| `app/ai/agent/evaluator.py` | Called by SelfCorrector only | Evaluate if its output is used |
| `compute_freshness(last_seen_at=...)` | `last_seen_at` param accepted, never used | Dead parameter |
| `PROVIDER_STATE` in job_pipeline.py | Superseded by Celery per-task isolation | Replace with Redis |
| `app/models/ai_models.py::AIPrompt` | Table created, no code reads/writes to it | Dead table |
| `job_pipeline.py` (entire module) | Should be retired in favor of scan_tasks.py | Architectural conflict |

---

## 9. Phased Fix Plan

### Phase 1 — Stop the Bleeding (P0 Fixes, Est. 1-2 days)

**Must fix before any new work. These crash the system.**

1. **P0-1:** Fix `process_company_jobs` return type — return `(metrics["fetched"], metrics["inserted"])` or restructure caller
2. **P0-2:** Replace `asyncio.get_event_loop().run_until_complete()` with `asyncio.run()` in both worker files
3. **P0-3:** Standardize decision labels — update `decision_engine.py` to use canonical `APPLY_NOW / TAILOR_RESUME_FIRST / SAVE_FOR_LATER` set
4. **P0-4:** Add `compute_fingerprint()` call and `fingerprint` field to `job_pipeline.py`'s jobs_to_insert
5. **P0-5:** Add vector column to `JobEmbedding` and `ResumeEmbedding` ORM models
6. **P1-1:** Fix `user_stats["success_rate"]` formula

### Phase 2 — Data Integrity & Dedup (P1 + Dedup, Est. 2-3 days)

7. Bulk-replace the 3000 N+1 queries in `_update_intelligence()` with 3 aggregated SQL queries
8. Fix CORS middleware to use `settings.CORS_ORIGINS`
9. Add `source_type` to `job_pipeline.py` jobs_to_insert
10. Fix `CareerAgent._observe()` to filter by user's target roles at DB level
11. Replace hard company deactivation with exponential backoff + max retry cap
12. Fix `_check_source_signals` to join Job table instead of checking free-text notes
13. Add Redis-backed circuit breaker replacing module-level `PROVIDER_STATE`
14. Run missing index migration

### Phase 3 — Embedding & Semantic Layer (P0-6, Est. 3-4 days)

15. Implement `app/services/embedding_service.py`:
    - Batch embedding generation via OpenAI `text-embedding-3-small`
    - Async upsert to `job_embeddings` table
    - Resume embedding on upload/update
16. Add Celery task: `generate_job_embeddings` — processes unembedded jobs in batches
17. Add semantic similarity endpoint: `GET /jobs/{id}/similar` using pgvector `<=>` cosine distance
18. Replace pure keyword matching in `resume_matcher` with hybrid: keyword (0.6 weight) + semantic (0.4 weight)

### Phase 4 — Learning Loop Hardening (P1, Est. 2-3 days)

19. Implement salary extraction via regex in `normalizer.py`
20. Granularize freshness labels (add `active_week`, `active_2_weeks`, `expired`)
21. Fix `compute_freshness()` repost detection logic
22. Add Redis watermark persistence for realtime monitor
23. Pick one scheduler (Celery Beat) and retire APScheduler to eliminate dual-scheduling

### Phase 5 — Scalability & Safety (P2, Est. 2-3 days)

24. Add concurrency cap to `_run_agent_all()` task dispatch
25. Move `check_robots_txt` result to Redis cache with 24h TTL
26. Fix `BaseConnector.fetch()` jitter to only apply on retries
27. Add alembic migration for all missing indexes
28. Implement pipeline mutex in Redis to prevent dual-pipeline race

---

## 10. Risk & Tradeoff Summary

| Fix | Risk | Tradeoff |
|-----|------|----------|
| Standardize decision labels | Medium — requires data migration for existing rows | Existing AIDecision rows in DB will have old labels. Run: `UPDATE ai_decisions SET decision = 'APPLY_NOW' WHERE decision = 'APPLY'` before deploying |
| Replace job_pipeline.py | Medium — APScheduler and realtime_monitor reference it | Keep as import-source for shared helpers (`_passes_quality_gate`, `_safe_json`, etc.) but remove as pipeline entrypoint |
| asyncio.run() in Celery | Low | May create a new event loop per task (intended behavior); ensure no shared state between tasks |
| Bulk intelligence update | Low | Slightly more complex query; add DB explain plan check |
| Redis circuit breaker | Medium | Adds Redis as a hard dependency for scan tasks; handle Redis-unavailable gracefully |
| Embedding generation | High cost risk | Gate embeddings behind `OPENAI_API_KEY` check; add per-job cost estimate before batching; start with Tier-1 jobs only |

---

**Bottom line:** The system has strong architectural intentions and good observability scaffolding, but 6 runtime-crashing bugs mean the core ingestion and learning pipelines are non-functional as written. Fix P0s first, then P1s, then implement the embedding layer. Do not add new features until Phase 1 is complete.
