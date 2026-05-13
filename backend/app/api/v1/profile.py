"""Profile-management endpoints used by the auto-apply agent."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User

router = APIRouter(prefix="/profile", tags=["profile"])


class ProfileOut(BaseModel):
    email: str
    full_name: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None
    current_location: Optional[str] = None
    work_authorization: Optional[str] = None
    min_salary: Optional[int] = None
    open_to_remote: Optional[bool] = True
    target_roles: Optional[list[str]] = None
    target_locations: Optional[list[str]] = None
    # Extra fields the auto-apply agent fills
    phone: Optional[str] = None
    years_of_experience: Optional[int] = None


class ProfileIn(BaseModel):
    full_name: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None
    current_location: Optional[str] = None
    work_authorization: Optional[str] = None
    min_salary: Optional[int] = None
    open_to_remote: Optional[bool] = None
    target_roles: Optional[list[str]] = None
    target_locations: Optional[list[str]] = None
    phone: Optional[str] = None
    years_of_experience: Optional[int] = None


@router.get("", response_model=ProfileOut)
async def get_profile(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # phone & years_of_experience aren't on the User model yet — stash in target_locations
    # short-term hack to avoid an ALTER TABLE; if needed we'll add real columns later.
    return ProfileOut(
        email=current_user.email,
        full_name=current_user.full_name,
        linkedin_url=current_user.linkedin_url,
        github_url=current_user.github_url,
        portfolio_url=current_user.portfolio_url,
        current_location=current_user.current_location,
        work_authorization=current_user.work_authorization,
        min_salary=current_user.min_salary,
        open_to_remote=current_user.open_to_remote,
        target_roles=current_user.target_roles or [],
        target_locations=current_user.target_locations or [],
    )


@router.patch("", response_model=ProfileOut)
async def update_profile(
    body: ProfileIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    fields = body.model_dump(exclude_unset=True)
    for k, v in fields.items():
        if hasattr(current_user, k):
            setattr(current_user, k, v)
    await db.commit()
    await db.refresh(current_user)
    return ProfileOut(
        email=current_user.email,
        full_name=current_user.full_name,
        linkedin_url=current_user.linkedin_url,
        github_url=current_user.github_url,
        portfolio_url=current_user.portfolio_url,
        current_location=current_user.current_location,
        work_authorization=current_user.work_authorization,
        min_salary=current_user.min_salary,
        open_to_remote=current_user.open_to_remote,
        target_roles=current_user.target_roles or [],
        target_locations=current_user.target_locations or [],
    )
