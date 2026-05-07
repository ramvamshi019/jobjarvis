import asyncio
import httpx

async def test_filter():
    url_us = "http://127.0.0.1:8000/api/jobs?country=US"
    url_in = "http://127.0.0.1:8000/api/jobs?country=IN"
    async with httpx.AsyncClient() as client:
        r_us = await client.get(url_us)
        r_in = await client.get(url_in)
        
        # Assume 401 because we aren't sending token, wait, let's print status
        print(f"US: {r_us.status_code}, IN: {r_in.status_code}")
        
        # If 401, we know the endpoint handles it. If 200, we check lengths
        if r_us.status_code == 200:
            us_jobs = r_us.json()
            in_jobs = r_in.json()
            us_countries = set(j.get('country') for j in us_jobs)
            in_countries = set(j.get('country') for j in in_jobs)
            print(f"US jobs distinct countries: {us_countries}")
            print(f"IN jobs distinct countries: {in_countries}")

asyncio.run(test_filter())
