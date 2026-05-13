import asyncio
from app.database import AsyncSessionLocal
from sqlalchemy import select
from app.models.user import User
from app.core.security import create_access_token
import httpx

async def test():
    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).limit(1))).scalar_one_or_none()
        if not user:
            print("No users found")
            return
        
        token = create_access_token({"sub": str(user.id)})
        
    async with httpx.AsyncClient() as client:
        # Test GET /api/jobs
        res = await client.get(
            "http://127.0.0.1:8000/api/v1/jobs",
            headers={"Authorization": f"Bearer {token}"}
        )
        print(f"GET /jobs status: {res.status_code}")
        if res.status_code == 200:
            jobs = res.json()
            print(f"Returned {len(jobs)} jobs. Top job decision: {jobs[0].get('decision')}, fit_score: {jobs[0].get('fit_score')}")
            
            job_id = jobs[0]['id']
            # Test GET /api/jobs/{id}/decision
            res2 = await client.get(
                f"http://127.0.0.1:8000/api/v1/jobs/{job_id}/decision",
                headers={"Authorization": f"Bearer {token}"}
            )
            print(f"GET /jobs/{job_id}/decision status: {res2.status_code}")
            if res2.status_code == 200:
                print(f"Decision: {res2.json()['decision']}")

asyncio.run(test())
