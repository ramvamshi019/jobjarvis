"""Evaluator — assesses quality of past decisions and outcomes."""
from datetime import datetime, timedelta, timezone
from typing import Optional
import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_models import AIDecision, AIDecisionFeedback, DecisionType
from app.models.application import Application

logger = structlog.get_logger(__name__)


class Evaluator:
    def __init__(self, db: AsyncSession, user_id: int):
        self.db = db
        self.user_id = user_id

    async def evaluate_recent_outcomes(self, days: int = 30) -> dict:
        """Evaluate AI decision quality over recent period."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # Get decisions with feedback
        q = await self.db.execute(
            select(AIDecision, AIDecisionFeedback).join(
                AIDecisionFeedback,
                AIDecisionFeedback.ai_decision_id == AIDecision.id,
                isouter=True
            ).where(
                and_(
                    AIDecision.user_id == self.user_id,
                    AIDecision.created_at >= cutoff,
                )
            )
        )
        rows = q.all()

        stats = {
            "total_decisions": 0,
            "apply_now_count": 0,
            "skip_count": 0,
            "feedback_count": 0,
            "positive_outcomes": 0,
            "negative_outcomes": 0,
            "interview_rate": 0.0,
            "decision_accuracy": 0.0,
            "avg_fit_score": 0.0,
            "corrections_needed": [],
        }

        fit_scores = []
        for dec, feedback in rows:
            stats["total_decisions"] += 1
            fit_scores.append(dec.fit_score or 0)

            # DecisionType is str+enum so comparisons work with both the enum
            # value and the raw string stored in the DB.
            if dec.decision == DecisionType.APPLY_NOW:
                stats["apply_now_count"] += 1
            elif dec.decision == DecisionType.SKIP:
                stats["skip_count"] += 1

            if feedback:
                stats["feedback_count"] += 1
                if feedback.outcome == "positive":
                    stats["positive_outcomes"] += 1
                elif feedback.outcome == "negative":
                    stats["negative_outcomes"] += 1

                # Detect bad decisions
                if dec.decision == DecisionType.APPLY_NOW and feedback.user_action == "skipped":
                    stats["corrections_needed"].append({
                        "type": "false_positive_apply",
                        "job_id": dec.job_id,
                        "fit_score": dec.fit_score,
                    })
                elif dec.decision == DecisionType.SKIP and feedback.user_action == "applied":
                    stats["corrections_needed"].append({
                        "type": "false_negative_skip",
                        "job_id": dec.job_id,
                        "fit_score": dec.fit_score,
                    })

        if fit_scores:
            stats["avg_fit_score"] = sum(fit_scores) / len(fit_scores)

        total_with_feedback = stats["positive_outcomes"] + stats["negative_outcomes"]
        if total_with_feedback > 0:
            stats["decision_accuracy"] = stats["positive_outcomes"] / total_with_feedback
            # interview_rate = interviews achieved / all jobs that received feedback
            # (not just apply_now_count which misses TAILOR_RESUME_FIRST applies)
            stats["interview_rate"] = stats["positive_outcomes"] / max(total_with_feedback, 1)

        return stats
