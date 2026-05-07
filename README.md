# JobJarvis — Autonomous AI Career Intelligence Platform

> **JobJarvis** monitors 40 000+ companies across six ATS platforms, ranks every fresh job against your resume, decides which ones are worth applying to, and guides you through the application — without ever submitting anything automatically.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Setup](#3-setup)
4. [Environment Variables](#4-environment-variables)
5. [Run Migrations](#5-run-migrations)
6. [Seed Companies](#6-seed-companies)
7. [Start Services](#7-start-services)
8. [Run a Manual Scan](#8-run-a-manual-scan)
9. [Run the CareerAgent](#9-run-the-careeragent)
10. [Run Tests](#10-run-tests)
11. [API Examples](#11-api-examples)
12. [Frontend Pages](#12-frontend-pages)
13. [Production Deployment](#13-production-deployment)

---

## 1. Overview

JobJarvis is a **production-grade autonomous job-search platform** built with:

| Layer | Technology |
|-------|-----------|
| API | FastAPI 0.111 + Python 3.11 |
| ORM | SQLAlchemy 2.0 async (asyncpg driver) |
| Database | PostgreSQL 16 + pgvector extension |
| Queue | Celery 5 + Redis 7 |
| AI | OpenAI GPT-4o / Anthropic Claude + custom rule-based classifiers |
| Frontend | Next.js 14 (App Router) + Tailwind CSS |

**Key capabilities:**

- **ATS Connectors** for Greenhouse, Lever, Ashby, SmartRecruiters, Workday (skeleton), iCIMS (skeleton)
- **Medallion Pipeline** — Bronze (raw) → Silver (normalised) → Gold (ranked + decided)
- **AI Layer** — role classifier, skill extractor, spam detector, work-auth detector, source classifier, resume matcher, interview probability estimator, decision engine
- **CareerAgent Brain** — Observe → Analyse → Decide → Act → Learn loop with persistent memory and self-correction
- **Application Copilot** — analyses job fit, drafts answers, writes recruiter outreach — **never auto-submits**
- **Human Review Queue** — AI decisions with confidence < 0.65 surface for your approval
- **Cost Gates** — daily AI budget cap, spam/freshness guards to minimise LLM spend

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         JobJarvis Platform                              │
│                                                                         │
│  ┌──────────┐    ┌─────────────────────────────────────────────────┐   │
│  │ Next.js  │◄──►│              FastAPI  (/api/v1/*)               │   │
│  │ Frontend │    │  auth · jobs · companies · resumes · decisions  │   │
│  └──────────┘    │  applications · agent · scans · reports · admin │   │
│                  └────────────────────┬────────────────────────────┘   │
│                                       │ async SQLAlchemy                │
│  ┌──────────────┐   ┌────────────┐   ▼                                 │
│  │ Celery Beat  │──►│   Redis    │  PostgreSQL 16 + pgvector           │
│  │  (scheduler) │   │  (broker)  │   ┌──────────────────────────────┐  │
│  └──────────────┘   └─────┬──────┘   │ Bronze  raw_jobs             │  │
│                            │          │ Silver  jobs (normalised)    │  │
│  ┌─────────────────────────▼──────┐  │ Gold    ai_decisions         │  │
│  │        Celery Workers          │  │         job_embeddings       │  │
│  │  scan_tasks · ai_tasks         │  └──────────────────────────────┘  │
│  │                                │                                     │
│  │  ┌──────────────────────────┐  │  ATS Connectors                    │
│  │  │   CareerAgent Brain      │  │  ├─ Greenhouse                     │
│  │  │  Observe→Analyse→Decide  │  │  ├─ Lever                         │
│  │  │  →Act→Learn              │  │  ├─ Ashby                         │
│  │  │  memory · planner        │  │  ├─ SmartRecruiters               │
│  │  │  evaluator · corrector   │  │  ├─ Workday  (skeleton)           │
│  │  └──────────────────────────┘  │  └─ iCIMS    (skeleton)           │
│  └────────────────────────────────┘                                    │
└─────────────────────────────────────────────────────────────────────────┘
```

### Scan Tiers (Celery Beat)

| Tier | Priority | Frequency |
|------|----------|-----------|
| 1 | ≥ 90 | Every hour |
| 2 | 60–89 | Every 6 hours |
| 3 | 20–59 | Daily at 02:00 UTC |

### Decision Types

| Decision | Meaning |
|----------|---------|
| `APPLY_NOW` | fit ≥ 75, role confidence ≥ 0.70, risk < 0.5, spam < 0.3 |
| `TAILOR_RESUME_FIRST` | fit ≥ 55 but skill match < 0.5 |
| `SAVE_FOR_LATER` | Decent fit, not urgent |
| `SKIP` | Low fit or spam |
| `HIGH_RISK` | Disqualifying work-auth flags (clearance, citizens-only) |
| `REVIEW_NEEDED` | AI confidence < 0.65 → human review queue |

---

## 3. Setup

### Prerequisites

- Docker ≥ 24 and Docker Compose v2
- (Local dev only) Python 3.11+, Node 20+, PostgreSQL 16 with pgvector

### Clone

```bash
git clone https://github.com/yourorg/jobjarvis.git
cd jobjarvis
```

### Copy environment file

```bash
cp .env.example .env
# Edit .env and fill in SECRET_KEY and at least one AI provider key
```

---

## 4. Environment Variables

See [`.env.example`](.env.example) for the full annotated list.

Minimum required values before first run:

| Variable | Description |
|----------|-------------|
| `SECRET_KEY` | JWT signing key — `openssl rand -hex 32` |
| `DATABASE_URL` | asyncpg connection string |
| `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` | At least one AI provider |

All other values have sensible defaults for local development.

---

## 5. Run Migrations

The `pgvector` and `pg_trgm` extensions are created automatically on first startup. Alembic handles the schema:

```bash
# Via Docker Compose (runs against the compose postgres)
docker compose run --rm backend alembic upgrade head

# Or locally (with DATABASE_URL exported)
cd backend
alembic upgrade head
```

To create a new migration after model changes:

```bash
alembic revision --autogenerate -m "describe_your_change"
alembic upgrade head
```

---

## 6. Seed Companies

Load the 103 pre-curated companies (FAANG, top AI labs, unicorns):

```bash
# Via Docker Compose
docker compose run --rm backend python scripts/seed_companies.py

# Or locally
cd backend
python scripts/seed_companies.py
```

The seed script is idempotent — safe to run multiple times.

---

## 7. Start Services

```bash
# Start everything (postgres, redis, backend, celery_worker, celery_beat, frontend)
docker compose up -d

# Watch logs
docker compose logs -f backend celery_worker

# Start with Flower monitoring dashboard (http://localhost:5555)
docker compose --profile monitoring up -d

# Shut down
docker compose down
```

Service URLs:

| Service | URL |
|---------|-----|
| Frontend | http://localhost:3000 |
| Backend API | http://localhost:8000 |
| API Docs (Swagger) | http://localhost:8000/docs |
| API Docs (ReDoc) | http://localhost:8000/redoc |
| Flower (with `--profile monitoring`) | http://localhost:5555 |

---

## 8. Run a Manual Scan

Trigger an immediate scan for a specific company via the API or Celery:

```bash
# Find the company id first
curl -s http://localhost:8000/api/v1/companies?search=Anthropic \
  -H "Authorization: Bearer $TOKEN" | jq '.[0].id'

# Trigger scan via API
curl -X POST http://localhost:8000/api/v1/scans/company/42 \
  -H "Authorization: Bearer $TOKEN"

# Or invoke the Celery task directly
docker compose exec celery_worker \
  celery -A app.workers.celery_app call \
  app.workers.scan_tasks.scan_company_task \
  --args='[42]'
```

Monitor scan progress:

```bash
curl http://localhost:8000/api/v1/scans/status \
  -H "Authorization: Bearer $TOKEN" | jq .
```

---

## 9. Run the CareerAgent

The CareerAgent runs automatically via Celery Beat (daily at 00:15 UTC). To trigger manually:

```bash
# Via API
curl -X POST http://localhost:8000/api/v1/agent/run \
  -H "Authorization: Bearer $TOKEN"

# Direct Celery invocation
docker compose exec celery_worker \
  celery -A app.workers.celery_app call \
  app.workers.ai_tasks.run_career_agent_task

# View the agent's last run summary
curl http://localhost:8000/api/v1/agent/status \
  -H "Authorization: Bearer $TOKEN" | jq .
```

---

## 10. Run Tests

```bash
# Local (Python 3.11 venv)
cd backend
pip install -r requirements.txt
pytest

# With coverage report
pytest --cov=app --cov-report=term-missing

# Run only fast unit tests (no integration marker)
pytest -m "not integration"

# Run a specific test file
pytest tests/test_normalizer.py -v

# Via Docker Compose
docker compose run --rm \
  -e DATABASE_URL=sqlite+aiosqlite:///:memory: \
  backend pytest
```

### Test suite overview

| File | What it tests |
|------|---------------|
| `test_normalizer.py` | Experience level classifier (intern/entry/mid/senior), salary extraction, location normalisation |
| `test_ai.py` | All 10 role categories, skill extractor, spam detector, work-auth detector, source classifier, resume matcher, decision agent |
| `test_dedup.py` | Three-level dedup; **critical**: SF ≠ NY fingerprints |
| `test_connectors.py` | All 6 ATS connectors (parse methods), connector registry |

---

## 11. API Examples

### Authentication

```bash
# Register
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"s3cur3!","full_name":"Jane Doe"}'

# Login → returns access_token
export TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"s3cur3!"}' | jq -r .access_token)
```

### Jobs

```bash
# List jobs with AI decisions (default: ranked by score desc)
curl "http://localhost:8000/api/v1/jobs?limit=20&decision=APPLY_NOW" \
  -H "Authorization: Bearer $TOKEN" | jq .

# Get single job
curl http://localhost:8000/api/v1/jobs/12345 \
  -H "Authorization: Bearer $TOKEN" | jq .

# Semantic similarity search (requires embeddings)
curl "http://localhost:8000/api/v1/jobs/search?q=senior+ML+engineer+remote&limit=10" \
  -H "Authorization: Bearer $TOKEN" | jq .
```

### Decisions

```bash
# List today's AI decisions
curl "http://localhost:8000/api/v1/decisions?date_from=$(date -u +%Y-%m-%d)" \
  -H "Authorization: Bearer $TOKEN" | jq .

# Submit feedback on a decision (thumbs up/down)
curl -X POST http://localhost:8000/api/v1/decisions/789/feedback \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"feedback":"positive","user_action":"applied","notes":"Great fit"}'
```

### Resumes

```bash
# Upload a resume (PDF or DOCX)
curl -X POST http://localhost:8000/api/v1/resumes/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/resume.pdf" \
  -F "version_name=Software Engineer v3"

# List resume versions
curl http://localhost:8000/api/v1/resumes \
  -H "Authorization: Bearer $TOKEN" | jq .
```

### Applications

```bash
# Analyse a job application (copilot — never auto-submits)
curl -X POST http://localhost:8000/api/v1/applications/copilot/analyse \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"job_id":12345,"resume_version_id":3}'

# Generate recruiter outreach message
curl -X POST http://localhost:8000/api/v1/outreach/generate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"job_id":12345,"message_type":"cold_email"}'
```

### Market Intelligence

```bash
# Current market trends
curl http://localhost:8000/api/v1/reports/market-trends \
  -H "Authorization: Bearer $TOKEN" | jq .

# Your skill gap report
curl http://localhost:8000/api/v1/reports/skill-gaps \
  -H "Authorization: Bearer $TOKEN" | jq .

# System observability snapshot (admin)
curl http://localhost:8000/api/v1/admin/observability \
  -H "Authorization: Bearer $TOKEN" | jq .
```

---

## 12. Frontend Pages

| Route | Description |
|-------|-------------|
| `/` | Landing / sign-in redirect |
| `/dashboard` | KPI cards, recent decisions, weekly plan |
| `/jobs` | Paginated job feed with filters (decision, role, remote, salary) |
| `/jobs/[id]` | Full job detail: description, AI scores, skill chips |
| `/jobs/new` | Jobs posted in the last 24 hours |
| `/jobs/apply` | Application copilot — answers + cover letter (manual submit only) |
| `/decisions` | Full AI decision history with feedback buttons |
| `/skills` | Skill gap radar chart + learning recommendations |
| `/resumes` | Upload and manage resume versions |
| `/tracker` | Kanban-style application tracker |
| `/market` | Market intelligence: top companies, roles, salary bands |
| `/review` | Human review queue (low-confidence AI decisions) |
| `/agent` | CareerAgent status, memory, weekly plan |
| `/admin` | System health, scan stats, data quality report |
| `/settings` | User preferences, work authorisation, notification settings |

All pages use the dark theme defined in `globals.css` and the collapsible `Sidebar` component.

---

## 13. Production Deployment

### Minimum recommended specs

| Service | CPU | RAM |
|---------|-----|-----|
| `backend` | 2 vCPU | 2 GB |
| `celery_worker` | 2 vCPU | 2 GB |
| `celery_beat` | 0.5 vCPU | 512 MB |
| `frontend` | 1 vCPU | 512 MB |
| `postgres` | 2 vCPU | 4 GB |
| `redis` | 1 vCPU | 1 GB |

### Checklist

- [ ] Set `ENVIRONMENT=production` and `LOG_LEVEL=WARNING`
- [ ] Generate a strong `SECRET_KEY`: `openssl rand -hex 32`
- [ ] Use managed Postgres (RDS, Cloud SQL, Supabase) with pgvector enabled
- [ ] Use managed Redis (ElastiCache, Upstash) with persistence on
- [ ] Store `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` in a secrets manager (AWS Secrets Manager, Vault, etc.)
- [ ] Set `AI_DAILY_BUDGET_USD` to a comfortable limit
- [ ] Configure `SMTP_*` for email notifications
- [ ] Set `SENTRY_DSN` for error tracking
- [ ] Put an HTTPS reverse proxy (nginx, Caddy, ALB) in front of both services
- [ ] Enable `RESPECT_ROBOTS_TXT=true`
- [ ] Run `alembic upgrade head` as a pre-deploy step (init container or migration job)
- [ ] Schedule regular Postgres backups

### Nginx example (minimal)

```nginx
server {
    listen 443 ssl;
    server_name jobjarvis.example.com;

    location /api/ {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location / {
        proxy_pass http://frontend:3000;
        proxy_set_header Host $host;
    }
}
```

### Scaling workers

To add more scan concurrency, scale the `celery_worker` service:

```bash
docker compose up -d --scale celery_worker=4
```

For large deployments (>10 000 companies), consider running separate worker containers for the `scans` and `ai` queues:

```bash
# Scan-only worker
celery -A app.workers.celery_app worker -Q scans --concurrency=8

# AI-only worker (lower concurrency due to LLM rate limits)
celery -A app.workers.celery_app worker -Q ai --concurrency=2
```

---

## Contributing

1. Fork the repo and create a feature branch.
2. Run `pytest` — all tests must pass.
3. Add/update tests for any new behaviour.
4. Open a PR with a clear description of what changed and why.

## License

MIT — see `LICENSE`.
