"""Company registry endpoints."""
import csv
import io
from typing import Optional
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, desc

from app.database import get_db
from app.core.dependencies import get_current_user, get_current_admin
from app.models.user import User
from app.models.company import Company
from app.schemas.company import CompanyIn, CompanyOut

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", response_model=list[CompanyOut])
async def list_companies(
    ats_type: Optional[str] = None,
    active: bool = True,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(Company).where(Company.active == active)
    if ats_type:
        q = q.where(Company.ats_type == ats_type)
    q = q.order_by(desc(Company.priority_score)).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    return list(result.scalars().all())


from app.connectors.ats import fetch_jobs_from_ats
import httpx

@router.post("", response_model=CompanyOut, status_code=201)
async def create_company(
    body: CompanyIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    async with httpx.AsyncClient(timeout=15.0) as client:
        jobs = []
        if body.ats_type and body.ats_identifier:
            try:
                jobs = await fetch_jobs_from_ats(client, body.ats_type.lower(), body.ats_identifier)
            except Exception:
                pass
        
        if not jobs and body.domain:
            try:
                from app.connectors.aggregator import fetch_jobs_from_aggregator
                jobs = await fetch_jobs_from_aggregator(client, body.domain)
            except Exception:
                pass
                
        if not jobs:
            raise HTTPException(status_code=400, detail="Strict Validation Failed: No jobs returned from ATS or Aggregator.")

    company = Company(**body.model_dump())
    _set_next_scan(company)
    db.add(company)
    await db.commit()
    await db.refresh(company)
    return company


@router.patch("/{company_id}", response_model=CompanyOut)
async def update_company(
    company_id: int,
    body: CompanyIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(company, k, v)
    await db.commit()
    await db.refresh(company)
    return company


@router.post("/import-csv")
async def import_companies_csv(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    created = 0
    skipped = 0

    async with httpx.AsyncClient(timeout=15.0) as client:
        for row in reader:
            name = (row.get("name") or "").strip()
            if not name:
                skipped += 1
                continue
            domain = (row.get("domain") or "").strip() or None
            ats_type = (row.get("ats_type") or "").strip() or None
            ats_id = (row.get("ats_identifier") or "").strip() or None

            # Check duplicate
            if domain:
                existing = await db.execute(select(Company).where(Company.domain == domain))
                if existing.scalar_one_or_none():
                    skipped += 1
                    continue

            # Strict Validation
            jobs = []
            if ats_type and ats_id:
                try:
                    jobs = await fetch_jobs_from_ats(client, ats_type.lower(), ats_id)
                except Exception:
                    pass
            
            if not jobs and domain:
                try:
                    from app.connectors.aggregator import fetch_jobs_from_aggregator
                    jobs = await fetch_jobs_from_aggregator(client, domain)
                except Exception:
                    pass
                    
            if not jobs:
                skipped += 1
                continue

            company = Company(
                name=name,
                domain=domain,
                career_url=(row.get("career_url") or "").strip() or None,
                ats_type=ats_type,
                ats_identifier=ats_id,
                country=(row.get("country") or "").strip() or None,
                priority_score=int(row.get("priority_score") or 50),
                scan_frequency_minutes=int(row.get("scan_frequency_minutes") or 360),
            )
            _set_next_scan(company)
            db.add(company)
            created += 1

    await db.commit()
    return {"created": created, "skipped": skipped}


def _set_next_scan(company: Company):
    company.next_scan_at = datetime.now(timezone.utc) + timedelta(
        minutes=company.scan_frequency_minutes
    )
