# JobJarvis

Autonomous job-search platform. FastAPI backend + Next.js frontend + Postgres (pgvector) + Redis + Celery workers, orchestrated by Docker Compose.

## Stack

| Layer | Tech | Path |
|------|------|------|
| Backend API | FastAPI (async, SQLAlchemy 2.0) | `backend/app/` |
| Workers | Celery (worker + beat) | `backend/app/workers/` |
| Frontend | Next.js + TypeScript + Tailwind | `frontend/` |
| Database | Postgres 16 + pgvector | docker service `postgres` |
| Cache / Broker | Redis 7 | docker service `redis` |
| Data transforms | dbt | `dbt/` |
| Optional orchestration | Apache Airflow 2.9 | `airflow/` |
| Browser extension | Chrome MV3 | `extension/` |
| Production deploy | Compose + Caddy | `deploy/` |

## Quick start (Docker, recommended)

```bash
cp .env.example .env
# Edit .env: at minimum set OPENAI_API_KEY (or ANTHROPIC_API_KEY) and SECRET_KEY
docker compose up -d --build
```

Once healthy:
- Frontend → http://localhost:3000
- API      → http://localhost:8000  (docs at `/docs`, health at `/api/health`)
- Postgres → localhost:5432  (user/pass/db: `jobjarvis`)
- Redis    → localhost:6379

Optional profiles:
```bash
docker compose --profile airflow    up -d   # Airflow at :8080  (admin / $AIRFLOW_ADMIN_PASSWORD)
docker compose --profile monitoring up -d   # Flower  at :5555
```

## Common commands

```bash
docker compose ps                                  # service status
docker compose logs -f backend                     # tail backend
docker compose logs -f celery_worker               # tail worker
docker compose exec backend alembic upgrade head   # run DB migrations
docker compose exec backend pytest                 # run backend tests
docker compose down                                # stop everything
docker compose down -v                             # stop + wipe volumes (FRESH DB)
```

## Local dev (no Docker for the app)

Run Postgres + Redis in Docker, run backend and frontend on the host.

```bash
# Infra only
docker compose up -d postgres redis

# Backend (terminal 1)
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --port 8000

# Frontend (terminal 2)
cd frontend
npm install
npm run dev          # http://localhost:3000
```

## Environment

`.env.example` documents all variables. Key ones:

- `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` — LLM access
- `EMBEDDING_BACKEND` — `sentence_transformers` (default, free, local) or `openai`
- `SECRET_KEY` — generate with `openssl rand -hex 32`
- `AI_DAILY_BUDGET_USD` — cap on LLM spend per day
- `SLACK_WEBHOOK_URL` — optional alerts

## Repo layout

```
backend/        FastAPI app, Celery workers, connectors, AI services, alembic migrations
frontend/       Next.js UI (App Router) with Tailwind
airflow/        Optional DAGs for batch orchestration
dbt/            Analytical models on top of the warehouse
deploy/         Production compose, Caddy config, deployment scripts
extension/      Chrome extension for in-browser capture
docker-compose.yml   Dev / single-host prod compose
_legacy/        Archived prototype (safe to delete: rm -rf _legacy)
```

## Notes

- The `_legacy/` folder holds the old Streamlit prototype and demo screenshots. Nothing in the current system references it. Delete when you're sure: `rm -rf _legacy`.
- ATS connectors live in `backend/app/connectors/` (Greenhouse, Lever, Ashby, SmartRecruiters, iCIMS, Workday).
- AI decision pipeline: `backend/app/ai/` — `decision_engine.py`, `resume_matcher.py`, `agent/career_agent.py`.
- See `backend/REPRODUCE.md` for backend-specific reproduction steps.
