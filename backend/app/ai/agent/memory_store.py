"""Persistent AI memory store — reads/writes ai_memory table."""
from datetime import datetime, timezone
from typing import Optional
import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_models import AIMemory

logger = structlog.get_logger(__name__)

MEMORY_TYPES = {
    "skill_signal": "A skill that correlates with interviews/offers",
    "company_signal": "Company quality or hiring pattern signal",
    "role_signal": "Role category that performs well for this user",
    "seniority_signal": "Seniority level preference signal",
    "outcome_pattern": "General outcome pattern from applications",
    "correction": "Self-correction applied to scoring",
    "source_signal": "Job source quality signal (direct/staffing)",
}


class MemoryStore:
    def __init__(self, db: AsyncSession, user_id: int):
        self.db = db
        self.user_id = user_id

    async def add_memory(
        self,
        memory_type: str,
        insight: str,
        evidence: dict = None,
        weight: float = 1.0,
    ) -> AIMemory:
        mem = AIMemory(
            user_id=self.user_id,
            memory_type=memory_type,
            insight=insight,
            evidence_json=evidence or {},
            weight=weight,
            is_active=True,
        )
        self.db.add(mem)
        await self.db.flush()
        logger.info("memory_added", user=self.user_id, type=memory_type, insight=insight[:60])
        return mem

    async def get_memories(self, memory_type: Optional[str] = None) -> list[AIMemory]:
        q = select(AIMemory).where(
            and_(AIMemory.user_id == self.user_id, AIMemory.is_active == True)
        ).order_by(AIMemory.weight.desc(), AIMemory.created_at.desc())
        if memory_type:
            q = q.where(AIMemory.memory_type == memory_type)
        result = await self.db.execute(q)
        return list(result.scalars().all())

    async def get_adjustments(self) -> dict:
        """Aggregate memories into scoring adjustments for the decision engine."""
        memories = await self.get_memories()
        adjustments = {
            "fit_adjustment": 0.0,
            "past_interview_rate": 0.15,
            "preferred_skills": [],
            "avoid_sources": [],
            "preferred_roles": [],
        }

        interview_outcomes = []
        for mem in memories:
            evidence = mem.evidence_json or {}

            if mem.memory_type == "skill_signal":
                skill = evidence.get("skill")
                if skill and evidence.get("outcome") == "positive":
                    adjustments["preferred_skills"].append(skill)

            elif mem.memory_type == "outcome_pattern":
                outcome = evidence.get("outcome")
                if outcome == "interview":
                    interview_outcomes.append(1)
                elif outcome in ("rejected", "no_response"):
                    interview_outcomes.append(0)

            elif mem.memory_type == "seniority_signal":
                if evidence.get("level") == "senior" and evidence.get("outcome") == "negative":
                    adjustments["fit_adjustment"] -= 5.0

            elif mem.memory_type == "source_signal":
                if evidence.get("source") == "STAFFING_AGENCY" and evidence.get("outcome") == "negative":
                    adjustments["avoid_sources"].append("STAFFING_AGENCY")

            elif mem.memory_type == "role_signal":
                if evidence.get("outcome") == "positive":
                    adjustments["preferred_roles"].append(evidence.get("role", ""))

        if interview_outcomes:
            adjustments["past_interview_rate"] = sum(interview_outcomes) / len(interview_outcomes)

        return adjustments

    async def update_memory_weight(self, memory_id: int, delta: float):
        result = await self.db.execute(
            select(AIMemory).where(AIMemory.id == memory_id)
        )
        mem = result.scalar_one_or_none()
        if mem:
            mem.weight = max(0.1, min(5.0, mem.weight + delta))
            mem.last_applied_at = datetime.now(timezone.utc)
            mem.applied_count += 1
            await self.db.flush()
