"""AI cost control — gate LLM calls, track spend, enforce daily limits."""
from datetime import datetime, date, timezone
from typing import Optional
import structlog
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.ai_models import AIUsageLog

logger = structlog.get_logger(__name__)


class CostController:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def should_call_llm(
        self,
        job: dict,
        reason: str = "",
    ) -> tuple[bool, str]:
        """
        Gate check: only call LLM when it's worth it.
        Returns (should_call, reason_if_blocked).
        """
        # Check spam — compare spam_score (0.0–1.0 probability) against the
        # dedicated AI_SPAM_THRESHOLD float, NOT against AI_MIN_FIT_SCORE_FOR_LLM
        # (which is a 0–100 integer fit-score gate and is semantically unrelated).
        spam_score = job.get("spam_score", 0)
        if spam_score >= settings.AI_SPAM_THRESHOLD:
            # Step 8: log every spam gate block for observability / tuning
            logger.info(
                "spam_gate_block",
                spam_score=round(spam_score, 3),
                threshold=settings.AI_SPAM_THRESHOLD,
                reason=reason,
            )
            return False, "spam_score_too_high"

        # Check role relevance
        role = job.get("role_category", "")
        if role in ("Not Relevant", "Other", None):
            return False, "role_not_relevant"

        # Check freshness (don't analyze stale jobs)
        freshness = job.get("freshness_label", "stale")
        if freshness == "stale":
            return False, "job_too_stale"

        # Check daily budget
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        cost_q = await self.db.execute(
            select(func.sum(AIUsageLog.estimated_cost)).where(
                AIUsageLog.created_at >= today_start
            )
        )
        today_cost = float(cost_q.scalar() or 0.0)

        if today_cost >= settings.AI_DAILY_COST_LIMIT_USD:
            return False, f"daily_budget_exceeded_${today_cost:.2f}"

        return True, ""

    async def log_usage(
        self,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        task_type: str,
        job_id: Optional[int] = None,
        user_id: Optional[int] = None,
        latency_ms: int = 0,
        reason: str = "",
    ) -> AIUsageLog:
        cost = (
            (input_tokens + output_tokens) / 1000.0
        ) * settings.AI_COST_PER_1K_TOKENS

        log = AIUsageLog(
            job_id=job_id,
            user_id=user_id,
            model_name=model_name,
            task_type=task_type,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=round(cost, 6),
            reason=reason[:500] if reason else None,
            latency_ms=latency_ms,
        )
        self.db.add(log)
        await self.db.flush()
        return log

    async def get_daily_cost(self) -> float:
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        q = await self.db.execute(
            select(func.sum(AIUsageLog.estimated_cost)).where(
                AIUsageLog.created_at >= today_start
            )
        )
        return float(q.scalar() or 0.0)
