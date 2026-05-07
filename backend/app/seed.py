"""
Startup seed data — populates the database with realistic sample jobs
so the API returns useful data immediately after first launch.

This module is idempotent: it checks whether data already exists before
inserting, so it is safe to call on every startup.
"""
import hashlib
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_context
from app.models.company import Company
from app.models.job import Job


# ── Sample companies ─────────────────────────────────────────────────────────
SAMPLE_COMPANIES = [
    {
        "name": "Anthropic",
        "domain": "anthropic.com",
        "ats_type": "greenhouse",
        "ats_identifier": "anthropic",
        "country": "US",
        "industry": "AI/ML",
        "priority_score": 95,
        "size_range": "mid",
    },
    {
        "name": "Databricks",
        "domain": "databricks.com",
        "ats_type": "greenhouse",
        "ats_identifier": "databricks",
        "country": "US",
        "industry": "Data Platform",
        "priority_score": 90,
        "size_range": "large",
    },
    {
        "name": "OpenAI",
        "domain": "openai.com",
        "ats_type": "greenhouse",
        "ats_identifier": "openai",
        "country": "US",
        "industry": "AI/ML",
        "priority_score": 95,
        "size_range": "mid",
    },
]

# ── Sample jobs ───────────────────────────────────────────────────────────────
# Each dict maps 1-to-1 with Job model fields (company_id injected at seed time)
SAMPLE_JOBS_TEMPLATE = [
    # ── Anthropic jobs ─────────────────────────────────────────────
    {
        "company_idx": 0,  # Anthropic
        "external_id": "seed-001",
        "title": "Senior Data Engineer",
        "normalized_title": "Senior Data Engineer",
        "location": "San Francisco, CA (Remote OK)",
        "normalized_location": "San Francisco, California",
        "country": "US",
        "remote_type": "hybrid",
        "job_url": "https://anthropic.com/careers/senior-data-engineer",
        "apply_url": "https://anthropic.com/careers/senior-data-engineer",
        "description": (
            "We are looking for a Senior Data Engineer to join Anthropic's Data Platform team. "
            "You will design and build scalable data pipelines that process terabytes of model "
            "training data, evaluation results, and product telemetry. "
            "\n\nRequired skills:\n"
            "- 5+ years of experience with Python and SQL\n"
            "- Strong experience with Apache Spark or PySpark\n"
            "- Experience with Airflow or similar orchestration tools\n"
            "- Proficiency with AWS (S3, Glue, Redshift)\n"
            "- Experience with dbt for data transformation\n"
            "- Knowledge of Delta Lake or Apache Iceberg\n"
            "\nPreferred:\n"
            "- Experience with Kafka for real-time pipelines\n"
            "- Familiarity with Databricks\n"
            "- Experience with ML data pipelines"
        ),
        "employment_type": "full_time",
        "experience_level": "senior",
        "role_category": "Data Engineer",
        "role_confidence": 0.95,
        "salary_min": 180000,
        "salary_max": 250000,
        "salary_currency": "USD",
        "salary_period": "annual",
        "required_skills": ["Python", "SQL", "Spark", "PySpark", "Airflow", "AWS", "dbt"],
        "preferred_skills": ["Kafka", "Databricks", "Delta Lake"],
        "matched_tools": ["Python", "SQL", "Spark", "PySpark", "Airflow", "AWS", "dbt", "Kafka", "Databricks", "Delta Lake"],
        "spam_score": 0.0,
        "eligibility_risk_score": 0.0,
        "source_type": "DIRECT_COMPANY",
        "source_confidence": 1.0,
        "freshness_label": "new_today",
        "active": True,
    },
    {
        "company_idx": 0,  # Anthropic
        "external_id": "seed-002",
        "title": "AI/ML Engineer",
        "normalized_title": "AI ML Engineer",
        "location": "Remote",
        "normalized_location": "Remote",
        "country": "US",
        "remote_type": "remote",
        "job_url": "https://anthropic.com/careers/ai-ml-engineer",
        "apply_url": "https://anthropic.com/careers/ai-ml-engineer",
        "description": (
            "Join Anthropic's model engineering team to build and improve state-of-the-art AI systems. "
            "\n\nRequired:\n"
            "- Strong Python programming skills\n"
            "- Experience with PyTorch or TensorFlow\n"
            "- Understanding of LLMs and transformer architectures\n"
            "- Experience with model training and fine-tuning\n"
            "- Knowledge of RAG systems and vector databases\n"
            "- Familiarity with LangChain or LlamaIndex\n"
            "\nPreferred:\n"
            "- Experience with Hugging Face transformers\n"
            "- Knowledge of MLflow for experiment tracking\n"
            "- Kubernetes experience for model deployment"
        ),
        "employment_type": "full_time",
        "experience_level": "mid",
        "role_category": "AI Engineer",
        "role_confidence": 0.92,
        "salary_min": 160000,
        "salary_max": 220000,
        "salary_currency": "USD",
        "salary_period": "annual",
        "required_skills": ["Python", "PyTorch", "LLMs", "RAG", "Vector Databases"],
        "preferred_skills": ["Hugging Face", "MLflow", "Kubernetes", "LangChain"],
        "matched_tools": ["Python", "PyTorch", "LLMs", "RAG", "Vector Databases", "LangChain"],
        "spam_score": 0.0,
        "eligibility_risk_score": 0.0,
        "source_type": "DIRECT_COMPANY",
        "source_confidence": 1.0,
        "freshness_label": "new_last_6_hours",
        "active": True,
    },
    {
        "company_idx": 0,  # Anthropic
        "external_id": "seed-003",
        "title": "MLOps Engineer",
        "normalized_title": "MLOps Engineer",
        "location": "San Francisco, CA",
        "normalized_location": "San Francisco, California",
        "country": "US",
        "remote_type": "hybrid",
        "job_url": "https://anthropic.com/careers/mlops-engineer",
        "apply_url": "https://anthropic.com/careers/mlops-engineer",
        "description": (
            "We need an MLOps Engineer to scale our model training and deployment infrastructure. "
            "\n\nRequired:\n"
            "- Experience with Kubernetes and Docker\n"
            "- Python programming proficiency\n"
            "- Experience with MLflow or similar experiment tracking\n"
            "- CI/CD pipeline experience (GitHub Actions, Jenkins)\n"
            "- AWS or GCP cloud infrastructure\n"
            "- Terraform for infrastructure as code\n"
            "\nPreferred:\n"
            "- Experience with Kubeflow or Vertex AI\n"
            "- Knowledge of model serving (TorchServe, Triton)\n"
            "- Experience with distributed training (PyTorch DDP, DeepSpeed)"
        ),
        "employment_type": "full_time",
        "experience_level": "mid",
        "role_category": "MLOps Engineer",
        "role_confidence": 0.90,
        "salary_min": 155000,
        "salary_max": 210000,
        "salary_currency": "USD",
        "salary_period": "annual",
        "required_skills": ["Kubernetes", "Docker", "Python", "MLflow", "CI/CD", "AWS", "Terraform"],
        "preferred_skills": ["Kubernetes", "PyTorch", "GCP"],
        "matched_tools": ["Kubernetes", "Docker", "Python", "MLflow", "CI/CD", "AWS", "Terraform", "GCP"],
        "spam_score": 0.0,
        "eligibility_risk_score": 0.0,
        "source_type": "DIRECT_COMPANY",
        "source_confidence": 1.0,
        "freshness_label": "new_today",
        "active": True,
    },
    # ── Databricks jobs ────────────────────────────────────────────
    {
        "company_idx": 1,  # Databricks
        "external_id": "seed-004",
        "title": "Data Platform Engineer",
        "normalized_title": "Data Platform Engineer",
        "location": "Remote, US",
        "normalized_location": "Remote",
        "country": "US",
        "remote_type": "remote",
        "job_url": "https://databricks.com/company/careers/data-platform-engineer",
        "apply_url": "https://databricks.com/company/careers/data-platform-engineer",
        "description": (
            "Databricks is looking for a Data Platform Engineer to build our lakehouse platform. "
            "\n\nRequired qualifications:\n"
            "- 4+ years Python and Scala experience\n"
            "- Deep expertise with Apache Spark\n"
            "- Experience with Delta Lake and medallion architecture\n"
            "- Proficiency with Databricks platform\n"
            "- Strong SQL skills\n"
            "- Experience with Kafka for streaming data\n"
            "\nBonus points:\n"
            "- dbt experience\n"
            "- Apache Flink knowledge\n"
            "- Snowflake or BigQuery experience"
        ),
        "employment_type": "full_time",
        "experience_level": "mid",
        "role_category": "Data Platform Engineer",
        "role_confidence": 0.93,
        "salary_min": 170000,
        "salary_max": 240000,
        "salary_currency": "USD",
        "salary_period": "annual",
        "required_skills": ["Python", "Scala", "Spark", "Delta Lake", "Databricks", "SQL", "Kafka"],
        "preferred_skills": ["dbt", "Flink", "Snowflake", "BigQuery"],
        "matched_tools": ["Python", "Scala", "Spark", "Delta Lake", "Databricks", "SQL", "Kafka", "dbt"],
        "spam_score": 0.0,
        "eligibility_risk_score": 0.0,
        "source_type": "DIRECT_COMPANY",
        "source_confidence": 1.0,
        "freshness_label": "new_last_3_days",
        "active": True,
    },
    {
        "company_idx": 1,  # Databricks
        "external_id": "seed-005",
        "title": "Analytics Engineer",
        "normalized_title": "Analytics Engineer",
        "location": "New York, NY (Hybrid)",
        "normalized_location": "New York, New York",
        "country": "US",
        "remote_type": "hybrid",
        "job_url": "https://databricks.com/company/careers/analytics-engineer",
        "apply_url": "https://databricks.com/company/careers/analytics-engineer",
        "description": (
            "Join Databricks as an Analytics Engineer to own our business intelligence stack. "
            "\n\nYou have:\n"
            "- 3+ years experience with dbt\n"
            "- Strong SQL and Python skills\n"
            "- Experience with Snowflake or BigQuery\n"
            "- Knowledge of data modeling best practices\n"
            "- Experience building dashboards (Looker, Tableau, or similar)\n"
            "\nFamiliarity with:\n"
            "- Databricks SQL\n"
            "- Airflow for pipeline scheduling\n"
            "- Git and CI/CD practices"
        ),
        "employment_type": "full_time",
        "experience_level": "mid",
        "role_category": "Analytics Engineer",
        "role_confidence": 0.91,
        "salary_min": 140000,
        "salary_max": 190000,
        "salary_currency": "USD",
        "salary_period": "annual",
        "required_skills": ["dbt", "SQL", "Python", "Snowflake", "BigQuery"],
        "preferred_skills": ["Databricks", "Airflow", "CI/CD"],
        "matched_tools": ["dbt", "SQL", "Python", "Snowflake", "BigQuery", "Databricks", "Airflow"],
        "spam_score": 0.0,
        "eligibility_risk_score": 0.0,
        "source_type": "DIRECT_COMPANY",
        "source_confidence": 1.0,
        "freshness_label": "new_today",
        "active": True,
    },
    {
        "company_idx": 1,  # Databricks
        "external_id": "seed-006",
        "title": "Backend Engineer - Data Infrastructure",
        "normalized_title": "Backend Engineer",
        "location": "San Francisco, CA",
        "normalized_location": "San Francisco, California",
        "country": "US",
        "remote_type": "hybrid",
        "job_url": "https://databricks.com/company/careers/backend-engineer",
        "apply_url": "https://databricks.com/company/careers/backend-engineer",
        "description": (
            "We are building next-gen data infrastructure at Databricks. "
            "\n\nRequired:\n"
            "- 5+ years of backend engineering experience\n"
            "- Python and/or Java/Scala proficiency\n"
            "- Experience building high-throughput REST APIs with FastAPI or Django\n"
            "- Distributed systems knowledge\n"
            "- PostgreSQL and Redis experience\n"
            "- Docker and Kubernetes for deployment\n"
            "\nNice to have:\n"
            "- Apache Kafka experience\n"
            "- gRPC / microservices architecture\n"
            "- Celery for async task processing"
        ),
        "employment_type": "full_time",
        "experience_level": "senior",
        "role_category": "Backend Engineer",
        "role_confidence": 0.88,
        "salary_min": 165000,
        "salary_max": 230000,
        "salary_currency": "USD",
        "salary_period": "annual",
        "required_skills": ["Python", "FastAPI", "PostgreSQL", "Redis", "Docker", "Kubernetes"],
        "preferred_skills": ["Kafka", "Celery", "Java", "Scala"],
        "matched_tools": ["Python", "FastAPI", "PostgreSQL", "Redis", "Docker", "Kubernetes", "Kafka"],
        "spam_score": 0.0,
        "eligibility_risk_score": 0.0,
        "source_type": "DIRECT_COMPANY",
        "source_confidence": 1.0,
        "freshness_label": "new_last_6_hours",
        "active": True,
    },
    # ── OpenAI jobs ────────────────────────────────────────────────
    {
        "company_idx": 2,  # OpenAI
        "external_id": "seed-007",
        "title": "Machine Learning Engineer",
        "normalized_title": "Machine Learning Engineer",
        "location": "San Francisco, CA",
        "normalized_location": "San Francisco, California",
        "country": "US",
        "remote_type": "onsite",
        "job_url": "https://openai.com/careers/machine-learning-engineer",
        "apply_url": "https://openai.com/careers/machine-learning-engineer",
        "description": (
            "OpenAI is seeking a Machine Learning Engineer to work on our flagship models. "
            "\n\nWhat we look for:\n"
            "- Strong Python skills\n"
            "- Deep expertise in PyTorch and deep learning\n"
            "- Experience with large-scale distributed training\n"
            "- Knowledge of transformer architectures\n"
            "- Familiarity with RLHF and preference learning\n"
            "\nBonus:\n"
            "- Experience with Hugging Face ecosystem\n"
            "- MLflow or Weights & Biases for experiment tracking\n"
            "- CUDA programming experience"
        ),
        "employment_type": "full_time",
        "experience_level": "senior",
        "role_category": "ML Engineer",
        "role_confidence": 0.94,
        "salary_min": 200000,
        "salary_max": 370000,
        "salary_currency": "USD",
        "salary_period": "annual",
        "required_skills": ["Python", "PyTorch", "LLMs", "TensorFlow"],
        "preferred_skills": ["Hugging Face", "MLflow", "Kubernetes"],
        "matched_tools": ["Python", "PyTorch", "LLMs", "TensorFlow", "Hugging Face", "MLflow"],
        "spam_score": 0.0,
        "eligibility_risk_score": 0.0,
        "source_type": "DIRECT_COMPANY",
        "source_confidence": 1.0,
        "freshness_label": "new_today",
        "active": True,
    },
    {
        "company_idx": 2,  # OpenAI
        "external_id": "seed-008",
        "title": "Data Engineer - Product Analytics",
        "normalized_title": "Data Engineer",
        "location": "San Francisco, CA (Hybrid)",
        "normalized_location": "San Francisco, California",
        "country": "US",
        "remote_type": "hybrid",
        "job_url": "https://openai.com/careers/data-engineer-product-analytics",
        "apply_url": "https://openai.com/careers/data-engineer-product-analytics",
        "description": (
            "Build the data infrastructure that powers OpenAI's product insights. "
            "\n\nMinimum qualifications:\n"
            "- 3+ years Python data engineering experience\n"
            "- Strong SQL proficiency\n"
            "- Airflow or Prefect for pipeline orchestration\n"
            "- BigQuery or Snowflake experience\n"
            "- dbt for transformations\n"
            "- Experience with streaming data (Kafka or Kinesis)\n"
            "\nPreferred qualifications:\n"
            "- AWS data services (S3, Glue, EMR)\n"
            "- Spark for large-scale processing\n"
            "- Experience with A/B testing infrastructure"
        ),
        "employment_type": "full_time",
        "experience_level": "mid",
        "role_category": "Data Engineer",
        "role_confidence": 0.92,
        "salary_min": 175000,
        "salary_max": 260000,
        "salary_currency": "USD",
        "salary_period": "annual",
        "required_skills": ["Python", "SQL", "Airflow", "BigQuery", "Snowflake", "dbt", "Kafka"],
        "preferred_skills": ["AWS", "Spark", "Kinesis"],
        "matched_tools": ["Python", "SQL", "Airflow", "BigQuery", "Snowflake", "dbt", "Kafka", "AWS", "Spark"],
        "spam_score": 0.0,
        "eligibility_risk_score": 0.0,
        "source_type": "DIRECT_COMPANY",
        "source_confidence": 1.0,
        "freshness_label": "new_last_3_days",
        "active": True,
    },
    {
        "company_idx": 2,  # OpenAI
        "external_id": "seed-009",
        "title": "AI Engineer - Agents Platform",
        "normalized_title": "AI Engineer",
        "location": "Remote",
        "normalized_location": "Remote",
        "country": "US",
        "remote_type": "remote",
        "job_url": "https://openai.com/careers/ai-engineer-agents",
        "apply_url": "https://openai.com/careers/ai-engineer-agents",
        "description": (
            "Help build the next generation of AI agents at OpenAI. "
            "\n\nRequired:\n"
            "- Expertise in Python\n"
            "- Experience building AI agents using LLMs\n"
            "- Familiarity with LangChain, LlamaIndex, or AutoGen\n"
            "- Understanding of RAG architecture and vector databases\n"
            "- Experience with OpenAI API or similar LLM APIs\n"
            "- FastAPI for building API backends\n"
            "\nNice to have:\n"
            "- Prompt engineering expertise\n"
            "- Experience with Anthropic API\n"
            "- Knowledge of Embeddings and semantic search"
        ),
        "employment_type": "full_time",
        "experience_level": "mid",
        "role_category": "AI Engineer",
        "role_confidence": 0.96,
        "salary_min": 185000,
        "salary_max": 280000,
        "salary_currency": "USD",
        "salary_period": "annual",
        "required_skills": ["Python", "LLMs", "LangChain", "RAG", "Vector Databases", "OpenAI API", "FastAPI"],
        "preferred_skills": ["Prompt Engineering", "Anthropic API", "Embeddings"],
        "matched_tools": ["Python", "LLMs", "LangChain", "RAG", "Vector Databases", "FastAPI", "Embeddings"],
        "spam_score": 0.0,
        "eligibility_risk_score": 0.0,
        "source_type": "DIRECT_COMPANY",
        "source_confidence": 1.0,
        "freshness_label": "new_last_hour",
        "active": True,
    },
    {
        "company_idx": 2,  # OpenAI
        "external_id": "seed-010",
        "title": "Senior Backend Engineer - Platform",
        "normalized_title": "Senior Backend Engineer",
        "location": "San Francisco, CA",
        "normalized_location": "San Francisco, California",
        "country": "US",
        "remote_type": "hybrid",
        "job_url": "https://openai.com/careers/senior-backend-engineer-platform",
        "apply_url": "https://openai.com/careers/senior-backend-engineer-platform",
        "description": (
            "Join OpenAI's platform team to build highly reliable API infrastructure. "
            "\n\nRequired qualifications:\n"
            "- 5+ years backend engineering with Python\n"
            "- FastAPI or Django for API development\n"
            "- PostgreSQL and Redis at scale\n"
            "- Microservices and distributed systems experience\n"
            "- Docker and Kubernetes for container orchestration\n"
            "- CI/CD with GitHub Actions or similar\n"
            "\nPlus if you have:\n"
            "- gRPC experience\n"
            "- Celery or similar async task queues\n"
            "- AWS cloud services"
        ),
        "employment_type": "full_time",
        "experience_level": "senior",
        "role_category": "Backend Engineer",
        "role_confidence": 0.89,
        "salary_min": 190000,
        "salary_max": 300000,
        "salary_currency": "USD",
        "salary_period": "annual",
        "required_skills": ["Python", "FastAPI", "PostgreSQL", "Redis", "Docker", "Kubernetes", "CI/CD"],
        "preferred_skills": ["Celery", "AWS", "Go"],
        "matched_tools": ["Python", "FastAPI", "PostgreSQL", "Redis", "Docker", "Kubernetes", "CI/CD", "AWS"],
        "spam_score": 0.0,
        "eligibility_risk_score": 0.0,
        "source_type": "DIRECT_COMPANY",
        "source_confidence": 1.0,
        "freshness_label": "new_today",
        "active": True,
    },
]


def _make_fingerprint(normalized_title: str, company_id: int, normalized_location: str) -> str:
    location_key = (normalized_location or "").split(",")[0].strip().lower()
    raw = f"{(normalized_title or '').lower()}::{company_id}::{location_key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


async def seed_sample_data() -> dict:
    """
    Seed companies and jobs if the database is empty.
    Safe to call on every startup — no-ops if data already present.
    Returns a dict with counts of what was created.
    """
    async with get_db_context() as db:
        # Check if jobs already exist
        job_count_q = await db.execute(select(func.count(Job.id)))
        existing_jobs = job_count_q.scalar() or 0
        if existing_jobs > 0:
            return {"seeded": False, "reason": "data_already_exists", "jobs": existing_jobs}

        now = datetime.now(timezone.utc)
        companies_created = []

        # Create sample companies
        for co_data in SAMPLE_COMPANIES:
            # Check if company already exists by domain
            existing = await db.execute(
                select(Company).where(Company.domain == co_data["domain"])
            )
            existing_co = existing.scalar_one_or_none()
            if existing_co:
                companies_created.append(existing_co)
                continue

            company = Company(
                name=co_data["name"],
                domain=co_data["domain"],
                ats_type=co_data.get("ats_type"),
                ats_identifier=co_data.get("ats_identifier"),
                country=co_data.get("country"),
                industry=co_data.get("industry"),
                priority_score=co_data.get("priority_score", 50),
                size_range=co_data.get("size_range"),
                active=True,
                next_scan_at=now + timedelta(hours=1),
            )
            db.add(company)
            await db.flush()
            companies_created.append(company)

        # Create sample jobs
        jobs_created = 0
        freshness_offsets = {
            "new_last_hour": timedelta(minutes=30),
            "new_last_6_hours": timedelta(hours=3),
            "new_today": timedelta(hours=12),
            "new_last_3_days": timedelta(days=2),
        }

        for job_data in SAMPLE_JOBS_TEMPLATE:
            company_idx = job_data.pop("company_idx")
            company = companies_created[company_idx]

            freshness = job_data.get("freshness_label", "new_today")
            first_seen_delta = freshness_offsets.get(freshness, timedelta(hours=12))
            first_seen_at = now - first_seen_delta

            fingerprint = _make_fingerprint(
                job_data.get("normalized_title", ""),
                company.id,
                job_data.get("normalized_location", ""),
            )

            job = Job(
                company_id=company.id,
                company_name=company.name,
                external_id=job_data.get("external_id"),
                title=job_data["title"],
                normalized_title=job_data.get("normalized_title"),
                location=job_data.get("location"),
                normalized_location=job_data.get("normalized_location"),
                country=job_data.get("country"),
                remote_type=job_data.get("remote_type"),
                job_url=job_data.get("job_url"),
                apply_url=job_data.get("apply_url"),
                description=job_data.get("description"),
                employment_type=job_data.get("employment_type"),
                experience_level=job_data.get("experience_level"),
                role_category=job_data.get("role_category"),
                role_confidence=job_data.get("role_confidence"),
                salary_min=job_data.get("salary_min"),
                salary_max=job_data.get("salary_max"),
                salary_currency=job_data.get("salary_currency", "USD"),
                salary_period=job_data.get("salary_period"),
                required_skills=job_data.get("required_skills", []),
                preferred_skills=job_data.get("preferred_skills", []),
                matched_tools=job_data.get("matched_tools", []),
                spam_score=job_data.get("spam_score", 0.0),
                spam_flags_json={"flags": []},
                work_auth_flags_json={"flags": []},
                eligibility_risk_score=job_data.get("eligibility_risk_score", 0.0),
                source_type=job_data.get("source_type", "DIRECT_COMPANY"),
                source_confidence=job_data.get("source_confidence", 1.0),
                freshness_label=freshness,
                fingerprint=fingerprint,
                raw_hash=fingerprint,  # reuse fingerprint as raw_hash for seeded data
                first_seen_at=first_seen_at,
                last_seen_at=now,
                posted_at=first_seen_at,
                active=True,
                source="seed",
            )
            db.add(job)
            jobs_created += 1

        await db.commit()
        return {
            "seeded": True,
            "companies_created": len(companies_created),
            "jobs_created": jobs_created,
        }
