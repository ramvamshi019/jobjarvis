"""Self-correction engine — adjusts scoring based on outcome patterns."""
from datetime import datetime, timedelta, timezone
from typing import Optional
import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agent.memory_store import MemoryStore
from app.ai.agent.evaluator import Evaluator
from app.models.ai_models import AIDecision, AIDecisionFeedback
from app.models.job import Job

logger = structlog.get_logger(__name__)


class SelfCorrector:
    """
    Analyzes feedback patterns and writes corrections to AI memory.
    Runs after each ingestion/decision cycle.
    """

    def __init__(self, db: AsyncSession, user_id: int):
        self.db = db
        self.user_id = user_id
        self.memory = MemoryStore(db, user_id)
        self.evaluator = Evaluator(db, user_id)

    async def run_corrections(self) -> list[dict]:
        """Run all correction checks. Returns list of corrections applied."""
        corrections = []

        stats = await self.evaluator.evaluate_recent_outcomes(days=30)

        # ── Correction 1: Too many rejections from senior roles ─────
        await self._check_seniority_corrections(corrections)

        # ── Correction 2: Skill signals from interviews ────────────
        await self._check_skill_signals(corrections)

        # ── Correction 3: Source quality signals ──────────────────
        await self._check_source_signals(corrections)

        # ── Correction 4: Low-fit jobs getting too many applies ────
        if stats["avg_fit_score"] < 55 and stats["apply_now_count"] > 10:
            await self.memory.add_memory(
                memory_type="correction",
                insight="Fit score threshold too low — increasing minimum from 55 to 65",
                evidence={"old_threshold": 55, "new_threshold": 65, "avg_fit": stats["avg_fit_score"]},
                weight=1.5,
            )
            corrections.append({"type": "fit_threshold_increase", "detail": "avg fit below 55"})

        logger.info("corrections_run", user=self.user_id, count=len(corrections))
        return corrections

    async def _check_seniority_corrections(self, corrections: list):
        """If senior roles have high rejection rate, lower senior score."""
        q = await self.db.execute(
            select(AIDecision, AIDecisionFeedback).join(
                AIDecisionFeedback, AIDecisionFeedback.ai_decision_id == AIDecision.id
            ).where(
                and_(
                    AIDecision.user_id == self.user_id,
                    AIDecisionFeedback.outcome == "negative",
                    AIDecision.created_at >= datetime.now(timezone.utc) - timedelta(days=30),
                )
            )
        )
        rows = q.all()
        senior_rejections = sum(
            1 for dec, fb in rows
            if dec.role_category and "senior" in str(dec.role_category).lower()
        )

        if senior_rejections >= 3:
            await self.memory.add_memory(
                memory_type="seniority_signal",
                insight=f"Senior roles had {senior_rejections} rejections — reduce senior role priority",
                evidence={"level": "senior", "outcome": "negative", "count": senior_rejections},
                weight=1.2,
            )
            corrections.append({"type": "seniority_down", "count": senior_rejections})

    async def _check_skill_signals(self, corrections: list):
        """If jobs requiring Skill X led to more interviews, increase that skill's weight."""
        q = await self.db.execute(
            select(AIDecision, AIDecisionFeedback).join(
                AIDecisionFeedback, AIDecisionFeedback.ai_decision_id == AIDecision.id
            ).where(
                and_(
                    AIDecision.user_id == self.user_id,
                    AIDecisionFeedback.user_action == "applied",
                    AIDecisionFeedback.outcome == "positive",
                    AIDecision.created_at >= datetime.now(timezone.utc) - timedelta(days=60),
                )
            )
        )
        rows = q.all()

        skill_interviews: dict[str, int] = {}
        for dec, _ in rows:
            for skill in (dec.matched_skills or []):
                skill_interviews[skill] = skill_interviews.get(skill, 0) + 1

        for skill, count in skill_interviews.items():
            if count >= 2:
                await self.memory.add_memory(
                    memory_type="skill_signal",
                    insight=f"Jobs requiring '{skill}' led to {count} positive outcomes — prioritize",
                    evidence={"skill": skill, "outcome": "positive", "count": count},
                    weight=1.0 + (count * 0.1),
                )
                corrections.append({"type": "skill_boost", "skill": skill})

    async def _check_source_signals(self, corrections: list):
        """If staffing agency jobs are yielding nothing, reduce their priority.

        The check joins AIDecision → Job so we can inspect job.source_type
        directly.  The previous implementation checked fb.feedback_notes for
        the literal string "staffing" — users never type that, so the
        correction could never fire.
        """
        # Join: AIDecisionFeedback → AIDecision → Job
        q = await self.db.execute(
            select(AIDecision, AIDecisionFeedback, Job)
            .join(AIDecisionFeedback, AIDecisionFeedback.ai_decision_id == AIDecision.id)
            .join(Job, Job.id == AIDecision.job_id)
            .where(
                and_(
                    AIDecision.user_id == self.user_id,
                    AIDecisionFeedback.outcome == "negative",
                    AIDecision.created_at >= datetime.now(timezone.utc) - timedelta(days=30),
                )
            )
        )
        rows = q.all()

        # Count negative outcomes from staffing-agency source postings.
        # source_type values set by classify_source / scan_tasks: "STAFFING_AGENCY".
        # Also handle lowercase variants stored by the realtime monitor.
        _staffing_values = {"staffing_agency", "staffing agency", "STAFFING_AGENCY"}
        staffing_negatives = sum(
            1 for _dec, _fb, job in rows
            if (job.source_type or "").strip() in _staffing_values
        )

        if staffing_negatives >= 3:
            await self.memory.add_memory(
                memory_type="source_signal",
                insight="Staffing agency postings performing poorly — reduce weighting",
                evidence={"source": "STAFFING_AGENCY", "outcome": "negative", "count": staffing_negatives},
                weight=1.3,
            )
            corrections.append({"type": "staffing_source_down"})
