"""
JobJarvis Airflow DAG — mirrors the Celery beat schedule with full visibility.

DAGs defined here:
  jobjarvis_scan_pipeline      — tiered ATS scanning (hourly/6h/daily)
  jobjarvis_discovery_pipeline — company discovery (daily quick + weekly full)
  jobjarvis_ml_pipeline        — embeddings + salary + dedup + spikes (daily)
  jobjarvis_intelligence       — career agent, data quality, market trends (daily)

All tasks call the Celery task by name via CeleryExecutor or a BashOperator
that fires `celery call`.  Adjust CELERY_BROKER_URL in the Airflow connection
if your broker differs.

To use: copy this file to ~/airflow/dags/ or mount it as a volume.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator

# ── Default args applied to all DAGs ─────────────────────────────────────────
DEFAULT_ARGS = {
    "owner": "jobjarvis",
    "depends_on_past": False,
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

CELERY_CMD = "celery -A app.workers.celery_app call"
WORKER_CONTAINER = "jobjarvis_celery_worker_1"  # adjust to your container name


def _celery_call(task_name: str, args: list | None = None) -> str:
    """Build a docker exec command to fire a Celery task."""
    args_str = f" --args '{args}'" if args else ""
    return (
        f"docker exec {WORKER_CONTAINER} "
        f"{CELERY_CMD} {task_name}{args_str}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# DAG 1: Scan pipeline
# ─────────────────────────────────────────────────────────────────────────────

with DAG(
    dag_id="jobjarvis_scan_pipeline",
    default_args=DEFAULT_ARGS,
    description="Tiered ATS job scanning",
    schedule_interval="0 * * * *",  # every hour
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=["jobjarvis", "scanning"],
) as scan_dag:

    scan_tier1 = BashOperator(
        task_id="scan_tier1",
        bash_command=_celery_call(
            "app.workers.scan_tasks.scan_tier_companies", ["tier1"]
        ),
    )

    scan_tier2 = BashOperator(
        task_id="scan_tier2",
        bash_command=_celery_call(
            "app.workers.scan_tasks.scan_tier_companies", ["tier2"]
        ),
    )

    scan_tier3 = BashOperator(
        task_id="scan_tier3",
        bash_command=_celery_call(
            "app.workers.scan_tasks.scan_tier_companies", ["tier3"]
        ),
    )

    embed_new = BashOperator(
        task_id="embed_new_jobs",
        bash_command=_celery_call("app.workers.embedding_tasks.embed_new_jobs"),
    )

    # Embed after scans complete
    [scan_tier1, scan_tier2, scan_tier3] >> embed_new


# ─────────────────────────────────────────────────────────────────────────────
# DAG 2: Company discovery pipeline
# ─────────────────────────────────────────────────────────────────────────────

with DAG(
    dag_id="jobjarvis_discovery_pipeline",
    default_args=DEFAULT_ARGS,
    description="Automated ATS company discovery",
    schedule_interval="0 1 * * *",  # daily at 1am
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=["jobjarvis", "discovery"],
) as discovery_dag:

    quick_discover = BashOperator(
        task_id="discover_companies_quick",
        bash_command=_celery_call("app.workers.discovery_tasks.discover_companies_quick"),
    )

    heal_failures = BashOperator(
        task_id="heal_failing_companies",
        bash_command=_celery_call("app.workers.healer_tasks.heal_failing_companies"),
    )

    quick_discover >> heal_failures


with DAG(
    dag_id="jobjarvis_discovery_full",
    default_args=DEFAULT_ARGS,
    description="Full weekly company discovery (all ATS platforms, SEC EDGAR, YC)",
    schedule_interval="0 6 * * 0",  # Sundays at 6am
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=["jobjarvis", "discovery"],
) as discovery_full_dag:

    full_discover = BashOperator(
        task_id="discover_companies_full",
        bash_command=_celery_call("app.workers.discovery_tasks.discover_companies_task"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# DAG 3: ML pipeline
# ─────────────────────────────────────────────────────────────────────────────

with DAG(
    dag_id="jobjarvis_ml_pipeline",
    default_args=DEFAULT_ARGS,
    description="Daily ML pipeline: salary prediction, deduplication, spike detection",
    schedule_interval="0 8 * * *",  # daily at 8am
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=["jobjarvis", "ml"],
) as ml_dag:

    predict_salaries = BashOperator(
        task_id="predict_missing_salaries",
        bash_command=_celery_call("app.workers.ml_tasks.predict_missing_salaries"),
    )

    dedup = BashOperator(
        task_id="deduplicate_jobs",
        bash_command=_celery_call("app.workers.ml_tasks.deduplicate_jobs"),
    )

    spikes = BashOperator(
        task_id="detect_hiring_spikes",
        bash_command=_celery_call("app.workers.ml_tasks.detect_hiring_spikes"),
    )

    # Run predict first, then dedup (so dupes don't waste salary inference),
    # then spikes (independent)
    predict_salaries >> dedup
    predict_salaries >> spikes


with DAG(
    dag_id="jobjarvis_train_models",
    default_args=DEFAULT_ARGS,
    description="Weekly model retraining",
    schedule_interval="0 5 * * 0",  # Sundays at 5am
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=["jobjarvis", "ml", "training"],
) as train_dag:

    train_salary = BashOperator(
        task_id="train_salary_model",
        bash_command=_celery_call("app.workers.ml_tasks.train_salary_model"),
    )

    backfill_embeddings = BashOperator(
        task_id="embed_all_jobs_backfill",
        bash_command=_celery_call("app.workers.embedding_tasks.embed_all_jobs"),
    )

    train_salary >> backfill_embeddings


# ─────────────────────────────────────────────────────────────────────────────
# DAG 4: Intelligence pipeline
# ─────────────────────────────────────────────────────────────────────────────

with DAG(
    dag_id="jobjarvis_intelligence",
    default_args=DEFAULT_ARGS,
    description="CareerAgent, data quality, market trends, self-correction",
    schedule_interval="15 * * * *",  # every hour at :15
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=["jobjarvis", "ai", "intelligence"],
) as intelligence_dag:

    career_agent = BashOperator(
        task_id="run_career_agent",
        bash_command=_celery_call("app.workers.ai_tasks.run_career_agent_all_users"),
    )

    data_quality = BashOperator(
        task_id="data_quality_check",
        bash_command=_celery_call("app.workers.ai_tasks.run_data_quality"),
    )

    market_intel = BashOperator(
        task_id="update_company_intelligence",
        bash_command=_celery_call("app.workers.ai_tasks.update_company_intelligence"),
    )

    self_correct = BashOperator(
        task_id="self_correction",
        bash_command=_celery_call("app.workers.ai_tasks.run_self_correction_all_users"),
    )

    # Career agent runs first (uses fresh jobs from scan);
    # quality + intelligence + correction run independently after
    career_agent >> [data_quality, market_intel, self_correct]
