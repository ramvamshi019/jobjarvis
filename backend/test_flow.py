import httpx
import asyncio

async def test_flow():
    client = httpx.AsyncClient(base_url="http://127.0.0.1:8000/api")
    
    # 1. Signup
    print("Signing up...")
    email = f"test_{asyncio.get_event_loop().time()}@example.com"
    r = await client.post("/auth/signup", json={"email": email, "password": "password", "full_name": "Test User"})
    assert r.status_code == 201, f"Signup failed: {r.text}"
    token = r.json()["access_token"]
    
    # 2. Login
    print("Logging in...")
    r = await client.post("/auth/login", json={"email": email, "password": "password"})
    assert r.status_code == 200, f"Login failed: {r.text}"
    token = r.json()["access_token"]
    
    client.headers["Authorization"] = f"Bearer {token}"
    
    # 3. /auth/me
    print("Checking /auth/me...")
    r = await client.get("/auth/me")
    assert r.status_code == 200, f"/auth/me failed: {r.text}"
    
    # 4. /jobs
    print("Checking /jobs...")
    r = await client.get("/jobs?limit=5")
    assert r.status_code == 200, f"/jobs failed: {r.text}"
    jobs = r.json()
    assert len(jobs) > 0, "No jobs returned!"
    assert "title" in jobs[0] and "company_name" in jobs[0], "Job fields incorrect!"
    
    print("All backend API tests passed successfully!")

asyncio.run(test_flow())
