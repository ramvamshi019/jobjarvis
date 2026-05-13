import asyncio
from app.services.job_pipeline import run_ingestion_pipeline
asyncio.run(run_ingestion_pipeline())
