"""Application tracker endpoints."""
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, desc

from app.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.models.application import Application, ApplicationAnswer
from app.schemas.application import ApplicationIn, ApplicationOut, ApplicationUpdate

router = APIRouter(prefix="/applications", tags=["applications"])


@router.get("", response_model=list[ApplicationOut])
async def list_applications(
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(Application).where(Application.user_id == current_user.id)
    if status:
        q = q.where(Application.status == status)
    q = q.order_by(desc(Application.created_at)).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    return list(result.scalars().all())


@router.post("", response_model=ApplicationOut, status_code=201)
async def create_application(
    body: ApplicationIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    app = Application(user_id=current_user.id, **body.model_dump())
    if body.status == "applied":
        app.applied_at = datetime.now(timezone.utc)
    db.add(app)
    await db.commit()
    await db.refresh(app)
    return app


@router.patch("/{app_id}", response_model=ApplicationOut)
async def update_application(
    app_id: int,
    body: ApplicationUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Application).where(and_(Application.id == app_id, Application.user_id == current_user.id))
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(app, k, v)
    if body.status == "applied" and not app.applied_at:
        app.applied_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(app)
    return app


@router.get("/answers")
async def list_answers(
    question_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(ApplicationAnswer).where(ApplicationAnswer.user_id == current_user.id)
    if question_type:
        q = q.where(ApplicationAnswer.question_type == question_type)
    result = await db.execute(q)
    return list(result.scalars().all())


@router.post("/answers", status_code=201)
async def create_answer(
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    answer = ApplicationAnswer(user_id=current_user.id, **body)
    db.add(answer)
    await db.commit()
    await db.refresh(answer)
    return answer
