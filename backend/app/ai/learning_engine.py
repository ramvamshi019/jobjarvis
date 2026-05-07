"""Skill gap engine and project recommender."""
from dataclasses import dataclass, field
from typing import Optional
import structlog
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_models import AIDecision

logger = structlog.get_logger(__name__)


@dataclass
class SkillGapItem:
    skill: str
    importance: str   # HIGH|MEDIUM|LOW
    frequency: int    # How often it appears in target jobs
    learning_plan: str
    project_suggestion: str
    estimated_days: int
    resume_tip: str


PROJECT_IDEAS = {
    "Kafka": {
        "project": "Real-time Job Alert Pipeline: Ingest job postings via Kafka producers, process with Spark Structured Streaming, store in PostgreSQL + Elasticsearch",
        "days": 14,
    },
    "dbt": {
        "project": "Analytics Warehouse: Build end-to-end dbt project with staging/intermediate/mart layers, tests, documentation, and CI/CD",
        "days": 10,
    },
    "Databricks": {
        "project": "Lakehouse Pipeline: Implement medallion architecture (Bronze/Silver/Gold) on Databricks with Delta Lake, Auto Loader, and DLT",
        "days": 14,
    },
    "RAG": {
        "project": "RAG Chatbot: Build document Q&A system with pgvector/Pinecone, FastAPI, LangChain, and streaming responses",
        "days": 7,
    },
    "LangChain": {
        "project": "AI Agent: Build a tool-using agent with LangChain, custom tools, memory, and FastAPI API layer",
        "days": 7,
    },
    "MLflow": {
        "project": "MLOps Pipeline: Train, track with MLflow, register model, serve with FastAPI, monitor drift",
        "days": 10,
    },
    "Kubernetes": {
        "project": "K8s ML Serving: Deploy FastAPI ML model to Kubernetes with HPA, liveness probes, and Prometheus metrics",
        "days": 12,
    },
    "Airflow": {
        "project": "Airflow Data Pipeline: Build multi-source ETL with dynamic DAGs, sensors, and dbt integration",
        "days": 10,
    },
    "Terraform": {
        "project": "IaC Data Platform: Provision S3, Glue, RDS, and Redshift using Terraform modules with state management",
        "days": 8,
    },
    "Spark": {
        "project": "Large-scale ETL: Build PySpark ETL processing 100M+ records with partitioning, broadcast joins, and Delta output",
        "days": 14,
    },
    "Snowflake": {
        "project": "Snowflake Analytics: Build cost-optimized analytics platform with Snowpark, streams, tasks, and dbt",
        "days": 10,
    },
}

LEARNING_RESOURCES = {
    "Kafka": "Official Confluent tutorials → Kafka Streams course → Practice: Kafka + Python",
    "dbt": "dbt Learn free courses → dbt Fundamentals cert → Build real project",
    "Databricks": "Databricks Academy free courses → Associate Developer cert path",
    "RAG": "LangChain docs → DeepLearning.AI RAG course → Build chatbot",
    "Kubernetes": "kubernetes.io tutorials → CKA exam prep → Minikube practice",
    "Airflow": "Astronomer courses → Official docs → Build 3 DAGs",
    "Spark": "Databricks Spark course → PySpark docs → Kaggle practice",
}


async def compute_skill_gaps(
    db: AsyncSession,
    user_id: int,
    resume_skills: list[str],
    target_roles: list[str] = None,
) -> list[SkillGapItem]:
    """Analyze recent job decisions to find most impactful skill gaps."""
    # Get missing skills from recent APPLY_NOW + TAILOR_RESUME_FIRST decisions
    q = await db.execute(
        select(AIDecision).where(
            and_(
                AIDecision.user_id == user_id,
                AIDecision.decision.in_(["APPLY_NOW", "TAILOR_RESUME_FIRST", "SAVE_FOR_LATER"]),
                AIDecision.missing_skills != None,
            )
        ).order_by(AIDecision.created_at.desc()).limit(100)
    )
    decisions = list(q.scalars().all())

    skill_freq: dict[str, int] = {}
    for dec in decisions:
        for skill in (dec.missing_skills or []):
            if skill not in resume_skills:
                skill_freq[skill] = skill_freq.get(skill, 0) + 1

    if not skill_freq:
        return []

    max_freq = max(skill_freq.values())
    gaps = []
    for skill, freq in sorted(skill_freq.items(), key=lambda x: -x[1])[:10]:
        importance = "HIGH" if freq >= max_freq * 0.7 else ("MEDIUM" if freq >= max_freq * 0.4 else "LOW")
        idea = PROJECT_IDEAS.get(skill, {"project": f"Build a portfolio project using {skill}", "days": 7})
        learning = LEARNING_RESOURCES.get(skill, f"Official {skill} documentation → online courses → practice project")

        gaps.append(SkillGapItem(
            skill=skill,
            importance=importance,
            frequency=freq,
            learning_plan=learning,
            project_suggestion=idea["project"],
            estimated_days=idea["days"],
            resume_tip=f"Add a dedicated '{skill}' project with quantified results to your resume",
        ))

    return gaps
