import asyncio
from app.database import AsyncSessionLocal
from sqlalchemy import select
from app.models.company import Company
import httpx
from datetime import datetime, timezone

async def test():
    from app.services.job_pipeline import process_company_jobs
    
    run_now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        c_obj = await db.get(Company, 1)  # Anthropic
        async with httpx.AsyncClient() as client:
            await process_company_jobs(db, c_obj, client, run_now)

asyncio.run(test())
