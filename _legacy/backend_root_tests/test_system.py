import asyncio
import logging
from sqlalchemy import select, func
from app.database import AsyncSessionLocal
from app.models.company import Company
from app.models.job import Job
from app.services.job_pipeline import run_ingestion_pipeline

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def validate_system():
    logger.info("Starting System Validation...")
    
    async with AsyncSessionLocal() as db:
        # 1. Check companies
        comp_count = await db.execute(select(func.count(Company.id)))
        comp_count = comp_count.scalar() or 0
        logger.info(f"📊 Companies Count: {comp_count}")
        
        # 2. Check jobs before
        job_count_before = await db.execute(select(func.count(Job.id)))
        job_count_before = job_count_before.scalar() or 0
        logger.info(f"📊 Jobs Count (Before): {job_count_before}")
        
    # 3. Run ingestion
    logger.info("🚀 Running Ingestion Pipeline...")
    await run_ingestion_pipeline()
    
    async with AsyncSessionLocal() as db:
        # 4. Check jobs after
        job_count_after = await db.execute(select(func.count(Job.id)))
        job_count_after = job_count_after.scalar() or 0
        logger.info(f"📊 Jobs Count (After): {job_count_after}")
        
        if job_count_after > job_count_before:
            logger.info(f"✅ Success! Inserted {job_count_after - job_count_before} new jobs.")
        else:
            logger.warning("⚠️ No new jobs inserted. (Maybe duplicates or fetch errors)")

    logger.info("🏁 System Validation Complete.")

if __name__ == "__main__":
    asyncio.run(validate_system())
