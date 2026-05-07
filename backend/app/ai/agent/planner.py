"""Weekly strategy planner — generates actionable weekly plan."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
import structlog
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job
from app.models.application import Application
from app.models.ai_models import AIDecision
from app.ai.agent.memory_store import MemoryStore

logger = structlog.get_logger(__name__)


@dataclass
class WeeklyPlan:
    week_start: str
    weekly_goal: str
    priority_roles: list[str] = field(default_factory=list)
    target_companies: list[str] = field(default_factory=list)
    skills_to_improve: list[str] = field(default_factory=list)
    resume_actions: list[str] = field(default_factory=list)
    application_targets: list[dict] = field(default_factory=list)
    project_recommendations: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


async def generate_weekly_plan(
    db: AsyncSession,
    user_id: int,
) -> WeeklyPlan:
    """Generate data-driven weekly career plan."""
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=now.weekday())).date().isoformat()

    memory_store = MemoryStore(db, user_id)
    adjustments = await memory_store.get_adjustments()

    # ── Recent applications ───────────────────────────────────────
    recent_apps_q = await db.execute(
        select(func.count(Application.id)).where(
            and_(
                Application.user_id == user_id,
                Application.applied_at >= now - timedelta(days=7),
            )
        )
    )
    recent_apps = recent_apps_q.scalar() or 0

    # ── Top decisions this week ───────────────────────────────────
    top_decisions_q = await db.execute(
        select(AIDecision).where(
            and_(
                AIDecision.user_id == user_id,
                AIDecision.decision == "APPLY_NOW",
                AIDecision.created_at >= now - timedelta(days=7),
            )
        ).order_by(AIDecision.fit_score.desc()).limit(5)
    )
    top_decisions = list(top_decisions_q.scalars().all())

    # ── Missing skills from recent decisions ──────────────────────
    all_missing = []
    for dec in top_decisions:
        if dec.missing_skills:
            all_missing.extend(dec.missing_skills)

    # Count frequency
    skill_freq: dict[str, int] = {}
    for skill in all_missing:
        skill_freq[skill] = skill_freq.get(skill, 0) + 1

    top_missing = sorted(skill_freq.items(), key=lambda x: -x[1])[:5]
    skills_to_improve = [s for s, _ in top_missing]

    # ── Priority roles from memory ─────────────────────────────────
    preferred_roles = adjustments.get("preferred_roles", [])
    if not preferred_roles:
        preferred_roles = ["Data Engineer", "AI Engineer", "ML Engineer"]

    # ── Application targets ────────────────────────────────────────
    application_targets = []
    for dec in top_decisions:
        result = await db.execute(select(Job).where(Job.id == dec.job_id))
        job = result.scalar_one_or_none()
        if job:
            application_targets.append({
                "job_id": job.id,
                "title": job.title,
                "company": job.company_name,
                "fit_score": dec.fit_score,
                "apply_within_hours": dec.apply_within_hours,
            })

    # ── Project recommendations ────────────────────────────────────
    project_recs = []
    for skill, _ in top_missing[:3]:
        project_recs.append(_skill_to_project(skill))

    # ── Resume actions ─────────────────────────────────────────────
    resume_actions = []
    if skills_to_improve:
        resume_actions.append(f"Add {skills_to_improve[0]} projects to resume")
    if recent_apps < 5:
        resume_actions.append("Review and update active resume version")

    weekly_goal = f"Apply to {max(10, 20 - recent_apps)} high-fit {'/'.join(preferred_roles[:2])} jobs"

    return WeeklyPlan(
        week_start=week_start,
        weekly_goal=weekly_goal,
        priority_roles=preferred_roles[:3],
        skills_to_improve=skills_to_improve,
        resume_actions=resume_actions,
        application_targets=application_targets,
        project_recommendations=[p for p in project_recs if p],
        metrics={
            "applications_this_week": recent_apps,
            "top_decisions_count": len(top_decisions),
        }
    )


def _skill_to_project(skill: str) -> str:
    project_map = {
        "Kafka": "Build a real-time job alert streaming pipeline with Kafka + Spark Structured Streaming",
        "dbt": "Build an analytics warehouse with dbt, Snowflake, and Airflow",
        "Databricks": "Build a lakehouse pipeline on Databricks with Delta Lake and Auto Loader",
        "RAG": "Build a RAG chatbot with pgvector, FastAPI, and LangChain",
        "LangChain": "Build an AI agent with LangChain + tool use for job search automation",
        "MLflow": "Build MLOps pipeline with MLflow tracking, model registry, and FastAPI serving",
        "Kubernetes": "Deploy a FastAPI ML service to Kubernetes with HPA and Prometheus metrics",
        "Airflow": "Build a multi-source data pipeline with Airflow DAGs and dbt transforms",
        "Terraform": "Provision AWS data infrastructure with Terraform (S3, Glue, RDS, Redshift)",
        "Spark": "Build a large-scale batch ETL with PySpark and Delta Lake on AWS EMR",
    }
    return project_map.get(skill, f"Build a portfolio project demonstrating {skill}")
