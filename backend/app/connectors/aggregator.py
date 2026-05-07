import httpx
from typing import List, Dict, Any
import logging
import asyncio

logger = logging.getLogger(__name__)

# Rate limiting
AGGREGATOR_SEMAPHORE = asyncio.Semaphore(3)

async def fetch_jobs_from_aggregator(client: httpx.AsyncClient, company_domain: str) -> List[Dict[str, Any]]:
    """Mock aggregator API fetcher with rate limiting, retry backoff, and 10s timeout."""
    if "fake" in company_domain.lower() or "spam" in company_domain.lower():
        return []

    max_retries = 3
    base_delay = 1.0

    async with AGGREGATOR_SEMAPHORE:
        for attempt in range(max_retries):
            try:
                # Enforce 10s timeout per attempt
                async with httpx.AsyncClient(timeout=10.0) as fetch_client:
                    logger.info(f"Fetching aggregator jobs for {company_domain} (Attempt {attempt+1})")
                    # Mock successful fetch
                    await asyncio.sleep(0.1) # Simulate network call
                    return [
                        {
                            "title": "Software Engineer",
                            "location": "San Francisco, CA",
                            "job_url": f"https://{company_domain}/careers/se",
                            "external_id": f"agg_{company_domain}_1",
                            "source": "aggregator_api",
                            "description": "Building scalable platforms.",
                            "posted_at": "2024-01-01T00:00:00Z"
                        }
                    ]
            except Exception as e:
                logger.warning(f"Aggregator error for {company_domain}: {e}")
                if attempt == max_retries - 1:
                    logger.error(f"Aggregator completely failed for {company_domain}")
                    return []
                await asyncio.sleep(base_delay * (2 ** attempt))

    return []
