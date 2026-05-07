"""CareerAgent endpoints."""
from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.ai.agent.career_agent import CareerAgent
from app.ai.agent.planner import generate_weekly_plan
from app.ai.agent.memory_store import MemoryStore
from app.models.ai_models import AIDecisionFeedback, AIDecision, HumanReviewQueue
from sqlalchemy import select, and_, desc
from typing import Optional

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/run")
async def run_agent(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Trigger CareerAgent run. Runs in foreground for API; use Celery for scheduled."""
    agent = CareerAgent(db, current_user.id)
    result = await agent.run()
    return result


@router.get("/weekly-plan")
async def weekly_plan(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    plan = await generate_weekly_plan(db, current_user.id)
    return plan


@router.get("/memory")
async def get_memory(
    memory_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    store = MemoryStore(db, current_user.id)
    memories = await store.get_memories(memory_type)
    return [
        {
            "id": m.id,
            "type": m.memory_type,
            "insight": m.insight,
            "weight": m.weight,
            "evidence": m.evidence_json,
            "created_at": m.created_at.isoformat(),
        }
        for m in memories
    ]


@router.post("/decisions/{decision_id}/feedback")
async def submit_feedback(
    decision_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    feedback = AIDecisionFeedback(
        ai_decision_id=decision_id,
        user_action=body.get("user_action", ""),
        outcome=body.get("outcome"),
        feedback_notes=body.get("feedback_notes"),
    )
    db.add(feedback)
    await db.commit()
    return {"message": "Feedback recorded", "decision_id": decision_id}


@router.get("/review-queue")
async def review_queue(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(HumanReviewQueue).where(
            and_(
                HumanReviewQueue.user_id == current_user.id,
                HumanReviewQueue.reviewed_at == None,
            )
        ).order_by(desc(HumanReviewQueue.created_at)).limit(50)
    )
    items = result.scalars().all()
    return [
        {
            "id": item.id,
            "job_id": item.job_id,
            "reason": item.reason,
            "confidence": item.confidence,
            "ai_decision": item.ai_decision_json,
            "created_at": item.created_at.isoformat(),
        }
        for item in items
    ]
