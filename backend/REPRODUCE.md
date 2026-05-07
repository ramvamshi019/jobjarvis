# JobJarvis Backend Setup & Execution

Follow these steps to set up the system from scratch with a clean PostgreSQL database.

## 1. Prerequisites
- PostgreSQL running on `localhost:5432`
- A database named `jobjarvis` owned by user `jobjarvis` (or update `app/config.py`)

## 2. Environment Setup
```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. Database Initialization & Migrations
```bash
# Drop and recreate schema (optional, for a clean start)
psql -U jobjarvis -d jobjarvis -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

# Run migrations
alembic upgrade head
```

## 4. Seeding Companies
```bash
python scripts/seed_companies.py
```

## 5. Running the System
You can run the scheduler which will automatically trigger the ingestion pipeline.

```bash
# Start the scheduler
python -m app.services.scheduler
```

Or run the ingestion pipeline manually for testing:
```bash
python test_system.py
```

## 6. Company Discovery (Optional)
To discover new companies from ATS registries:
```bash
python run_discovery.py 100
```

---

## Clean Architecture Overview

- **`app/config.py`**: Centralized configuration using Pydantic Settings. Enforces PostgreSQL.
- **`app/database.py`**: Async SQLAlchemy 2.0 setup. Handles PostgreSQL extensions gracefully.
- **`app/models/`**: Declarative models. `Base` is shared across all models.
- **`app/services/job_pipeline.py`**: The heart of the ingestion system. Handles fetching, enrichment, and safe bulk upserts with conflict handling.
- **`app/services/scheduler.py`**: APScheduler-based orchestration for periodic scans.
- **`app/services/company_discovery.py`**: Automated company detection and metadata enrichment.
