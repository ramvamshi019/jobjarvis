"""Compliance layer: robots.txt placeholder, domain blocklist, fetch audit."""
from typing import Optional
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.ai_models import FetchAuditLog

logger = structlog.get_logger(__name__)

# Domain blocklist — companies that have explicitly requested no scraping
DOMAIN_BLOCKLIST: set[str] = set()

def is_blocklisted(domain: str) -> bool:
    return domain.lower() in DOMAIN_BLOCKLIST

def add_to_blocklist(domain: str):
    DOMAIN_BLOCKLIST.add(domain.lower())
    logger.info("domain_blocklisted", domain=domain)

async def log_fetch(
    db: AsyncSession,
    company_id: int,
    domain: str,
    url: str,
    method: str = "GET",
    status_code: Optional[int] = None,
    response_time_ms: int = 0,
    success: bool = True,
    error_message: str = "",
    jobs_found: int = 0,
    jobs_new: int = 0,
):
    """Persist fetch audit log for compliance and debugging."""
    log = FetchAuditLog(
        company_id=company_id,
        domain=domain,
        url=url[:2000] if url else "",
        method=method,
        status_code=status_code,
        response_time_ms=response_time_ms,
        success=success,
        error_message=error_message[:1000] if error_message else None,
        jobs_found=jobs_found,
        jobs_new=jobs_new,
    )
    db.add(log)
    await db.flush()

async def check_robots_txt(domain: str) -> bool:
    """
    Placeholder: In production, fetch and parse robots.txt.
    Returns True if fetching is allowed.
    Career portal API endpoints are generally allowed.
    """
    return not is_blocklisted(domain)
