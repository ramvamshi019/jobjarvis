"""
CareerAgent — Autonomous AI Career Intelligence Brain.

Loop: Observe → Analyze → Decide → Act → Learn → Repeat
"""
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job
from app.models.ai_models import AIDecision, DecisionType, HumanReviewQueue, AIMemory
from app.models.resume import ResumeVersion
from app.models.user import User
from app.ai.role_classifier import classify_role
from app.ai.skill_extractor import extract_skills
from app.ai.spam_detector import detect_spam
from app.ai.source_classifier import classify_source
from app.ai.work_auth_detector import detect_work_auth
from app.ai.resume_matcher import compute_match
from app.ai.decision_agent import make_decision
from app.ai.agent.memory_store import MemoryStore
from app.ai.agent.self_corrector import SelfCorrector
from app.services.freshness import is_fresh_enough_for_ai
from app.config import settings

logger = structlog.get_logger(__name__)


class CareerAgent:
    """
    The main autonomous career intelligence loop.
    Each run processes new/unprocessed jobs for a user.
    """

    def __init__(self, db: AsyncSession, user_id: int):
        self.db = db
        self.user_id = user_id
        self.memory = MemoryStore(db, user_id)
        self.corrector = SelfCorrector(db, user_id)
        self._run_stats = {
            "jobs_analyzed": 0,
            "decisions_made": 0,
            "apply_now": 0,
            "skip": 0,
            "review_queue": 0,
            "errors": 0,
        }

    # ══════════════════════════════════════════════════════════════════
    # OBSERVE
    # ══════════════════════════════════════════════════════════════════
    async def _observe(self) -> tuple[list[Job], ResumeVersion, dict]:
        """Gather new jobs, active resume, and memory adjustments."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        # New jobs not yet decided
        decided_ids_q = await self.db.execute(
            select(AIDecision.job_id).where(AIDecision.user_id == self.user_id)
        )
        decided_ids = {row[0] for row in decided_ids_q.fetchall()}

        q = await self.db.execute(
            select(Job).where(
                and_(
                    Job.active == True,
                    Job.first_seen_at >= cutoff,
                    Job.role_category != "Not Relevant",
                )
            ).order_by(Job.first_seen_at.desc()).limit(200)
        )
        all_new_jobs = list(q.scalars().all())
        new_jobs = [j for j in all_new_jobs if j.id not in decided_ids]

        # Active resume
        resume_q = await self.db.execute(
            select(ResumeVersion).where(
                and_(ResumeVersion.user_id == self.user_id, ResumeVersion.is_active == True)
            ).limit(1)
        )
        resume = resume_q.scalar_one_or_none()

        # Memory adjustments
        adjustments = await self.memory.get_adjustments()

        logger.info("agent_observe", user=self.user_id, new_jobs=len(new_jobs), has_resume=resume is not None)
        return new_jobs, resume, adjustments

    # ══════════════════════════════════════════════════════════════════
    # ANALYZE
    # ══════════════════════════════════════════════════════════════════
    async def _analyze_job(self, job: Job, resume: Optional[ResumeVersion]) -> tuple[dict, dict]:
        """Classify, score, and analyze a single job."""
        job_dict = {
            "id": job.id,
            "title": job.title,
            "company_name": job.company_name,
            "description": job.description or "",
            "role_category": job.role_category,
            "experience_level": job.experience_level,
            "required_skills": job.required_skills or [],
            "preferred_skills": job.preferred_skills or [],
            "matched_tools": job.matched_tools or [],
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            "normalized_location": job.normalized_location,
            "remote_type": job.remote_type,
            "spam_score": job.spam_score,
            "eligibility_risk_score": job.eligibility_risk_score,
            "work_auth_flags_json": job.work_auth_flags_json or {},
            "source_type": job.source_type,
            "freshness_label": job.freshness_label or "active_unknown",
        }

        # Build resume profile
        if resume:
            resume_profile = {
                "skills": (resume.skills_json or {}).get("all", []),
                "target_roles": [],
                "experience_level": "mid",
                "tools": resume.tools_json or [],
                "cloud_platforms": resume.cloud_platforms_json or [],
            }
        else:
            resume_profile = {
                "skills": [], "target_roles": [], "experience_level": "mid",
                "tools": [], "cloud_platforms": [],
            }

        return job_dict, resume_profile

    # ══════════════════════════════════════════════════════════════════
    # DECIDE + ACT
    # ══════════════════════════════════════════════════════════════════
    async def _decide(
        self,
        job_dict: dict,
        resume_profile: dict,
        adjustments: dict,
        user: Optional[User] = None,
    ) -> AIDecision:
        user_prefs = {}
        if user:
            user_prefs = {
                "open_to_remote": user.open_to_remote,
                "target_locations": user.target_locations or [],
                "min_salary": user.min_salary or 0,
            }

        match = compute_match(job_dict, resume_profile, user_prefs)
        decision_output = make_decision(match, job_dict, user_prefs, adjustments)

        decision = AIDecision(
            user_id=self.user_id,
            job_id=job_dict["id"],
            decision=decision_output.decision,
            fit_score=decision_output.fit_score,
            role_category=decision_output.role_category,
            role_match_score=match.role_match_score,
            skill_match_score=match.skill_match_score,
            seniority_match_score=match.seniority_match_score,
            domain_match_score=match.domain_match_score,
            location_match_score=match.location_match_score,
            compensation_match_score=match.compensation_match_score,
            risk_score=match.risk_score,
            confidence=decision_output.confidence,
            interview_probability=decision_output.interview_probability,
            priority=decision_output.priority,
            matched_skills=decision_output.matched_skills,
            missing_skills=decision_output.missing_skills,
            risk_flags=decision_output.risk_flags,
            recommended_resume=decision_output.recommended_resume,
            why_apply=decision_output.why_apply,
            why_not=decision_output.why_not,
            application_strategy=decision_output.application_strategy,
            apply_within_hours=decision_output.apply_within_hours,
            resume_suggestions=decision_output.resume_suggestions,
            needs_human_review=decision_output.needs_human_review,
        )
        self.db.add(decision)
        await self.db.flush()

        # Queue for human review if needed
        if decision_output.needs_human_review:
            review = HumanReviewQueue(
                job_id=job_dict["id"],
                user_id=self.user_id,
                reason=f"AI confidence {decision_output.confidence:.0%} below threshold",
                confidence=decision_output.confidence,
                ai_decision_json={
                    "decision": decision_output.decision.value,  # Step 5: serialize enum → str
                    "fit_score": decision_output.fit_score,
                }
            )
            self.db.add(review)
            await self.db.flush()
            self._run_stats["review_queue"] += 1

        return decision

    # ══════════════════════════════════════════════════════════════════
    # LEARN
    # ══════════════════════════════════════════════════════════════════
    async def _learn(self) -> list[dict]:
        """Run self-correction and update memory."""
        corrections = await self.corrector.run_corrections()
        await self.db.commit()
        return corrections

    # ══════════════════════════════════════════════════════════════════
    # MAIN RUN LOOP
    # ══════════════════════════════════════════════════════════════════
    async def run(self) -> dict:
        """Execute full Observe→Analyze→Decide→Act→Learn cycle."""
        logger.info("agent_run_start", user=self.user_id)

        # OBSERVE
        new_jobs, resume, adjustments = await self._observe()

        # Get user preferences
        user_q = await self.db.execute(select(User).where(User.id == self.user_id))
        user = user_q.scalar_one_or_none()

        # ANALYZE + DECIDE + ACT
        for job in new_jobs:
            try:
                # Cost control: skip if not worth LLM processing
                if job.spam_score and job.spam_score >= 0.7:
                    continue
                if job.role_category in ("Not Relevant", "Other"):
                    continue

                job_dict, resume_profile = await self._analyze_job(job, resume)
                decision = await self._decide(job_dict, resume_profile, adjustments, user)

                self._run_stats["jobs_analyzed"] += 1
                self._run_stats["decisions_made"] += 1

                if decision.decision == DecisionType.APPLY_NOW:
                    self._run_stats["apply_now"] += 1
                elif decision.decision in (DecisionType.SKIP, DecisionType.HIGH_RISK):
                    self._run_stats["skip"] += 1

            except Exception as e:
                logger.error("agent_job_error", job_id=job.id, error=str(e))
                self._run_stats["errors"] += 1

        # Commit all decisions
        await self.db.commit()

        # LEARN
        corrections = await self._learn()

        result = {
            **self._run_stats,
            "corrections_applied": len(corrections),
            "run_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("agent_run_complete", **result)
        return result
