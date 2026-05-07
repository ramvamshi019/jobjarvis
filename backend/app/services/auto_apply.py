import asyncio
import datetime
import random
import structlog
from typing import List, Tuple, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, desc, func

from app.models.job import Job
from app.models.user import User
from app.models.ai_models import AIDecision, DecisionType
from app.models.application import Application, ApplicationStatus
from app.models.resume import ResumeVersion

logger = structlog.get_logger(__name__)

class AutoApplyEngine:
    """
    Production-grade Auto Apply Engine.
    Handles rate limits, apply classification, failure memory, and follow-ups.
    """

    def __init__(self, db: AsyncSession, user: User, strategy: str = "balanced"):
        self.db = db
        self.user = user
        self.strategy = strategy
        
        # Strategy limits
        self.max_hourly = 10
        if strategy == "aggressive":
            self.max_daily = 50
            self.threshold = 60
            self.max_hourly = 15
        elif strategy == "selective":
            self.max_daily = 10
            self.threshold = 80
            self.max_hourly = 5
        else: # balanced
            self.max_daily = 20
            self.threshold = 70
            
    async def get_eligible_jobs(self) -> List[Tuple[Job, AIDecision]]:
        # Step 1: Base filtration
        stmt = select(Job, AIDecision).join(AIDecision).where(
            and_(
                AIDecision.user_id == self.user.id,
                AIDecision.decision == DecisionType.APPLY_NOW,
                AIDecision.fit_score >= self.threshold,
                AIDecision.confidence >= 0.5,
                Job.spam_score < 50,
                Job.active == True
            )
        ).order_by(desc(AIDecision.fit_score))
        
        result = await self.db.execute(stmt)
        candidates = result.all()
        
        # Step 2: Skip already processed
        stmt_app = select(Application.job_id).where(Application.user_id == self.user.id)
        res_app = await self.db.execute(stmt_app)
        applied_job_ids = set(res_app.scalars().all())
        
        # Step 3: Failure Memory (Company Rejection Rate)
        # We query recent rejections to penalize companies that auto-reject us
        stmt_rej = select(Job.company_name, func.count(Application.id)).select_from(Application).join(Job).where(
            and_(
                Application.user_id == self.user.id,
                Application.status == ApplicationStatus.REJECTED
            )
        ).group_by(Job.company_name)
        
        res_rej = await self.db.execute(stmt_rej)
        rejection_counts = {row[0]: row[1] for row in res_rej.all() if row[0]}
        
        filtered_candidates = []
        for job, dec in candidates:
            if job.id in applied_job_ids:
                continue
            
            # Dynamic Failure Memory Check
            comp_rejections = rejection_counts.get(job.company_name, 0)
            if comp_rejections >= 3:
                # Require much higher threshold if they frequently reject
                if dec.fit_score < (self.threshold + 10):
                    logger.debug("auto_apply.skipped_due_to_failure_memory", company=job.company_name)
                    continue
                    
            filtered_candidates.append((job, dec))
            
        return filtered_candidates

    async def run(self):
        logger.info("auto_apply.start", user_id=self.user.id, strategy=self.strategy)
        
        candidates = await self.get_eligible_jobs()
        if not candidates:
            logger.info("auto_apply.no_candidates")
            return
            
        # Rate Limiting: Daily and Hourly checks
        now = datetime.datetime.now(datetime.timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        hour_ago = now - datetime.timedelta(hours=1)
        
        stmt_limits = select(Application.created_at).where(
            and_(
                Application.user_id == self.user.id,
                Application.created_at >= today
            )
        )
        res_limits = await self.db.execute(stmt_limits)
        apps_today = res_limits.scalars().all()
        
        count_today = len(apps_today)
        count_hourly = len([a for a in apps_today if a >= hour_ago])
        
        rem_daily = self.max_daily - count_today
        rem_hourly = self.max_hourly - count_hourly
        
        limit = min(rem_daily, rem_hourly)
        if limit <= 0:
            logger.info("auto_apply.rate_limited", count_today=count_today, count_hourly=count_hourly)
            return
            
        jobs_to_apply = candidates[:limit]
        logger.info("auto_apply.batch_started", count=len(jobs_to_apply))
        
        batch_count = 0
        success_count = 0
        manual_queue_count = 0
        
        for job, decision in jobs_to_apply:
            res = await self._apply_to_job(job, decision)
            batch_count += 1
            if res in (ApplicationStatus.APPLIED, ApplicationStatus.FORM_PENDING):
                success_count += 1
            elif res == ApplicationStatus.MANUAL_REQUIRED:
                manual_queue_count += 1
            
            # Advanced Rate Limiting
            if batch_count < len(jobs_to_apply):
                if batch_count % 10 == 0:
                    pause = random.uniform(300.0, 600.0) # 5-10 minutes
                    logger.info("auto_apply.batch_pause", duration=pause)
                    await asyncio.sleep(pause)
                else:
                    delay = random.uniform(20.0, 90.0)
                    logger.debug("auto_apply.inter_delay", duration=delay)
                    await asyncio.sleep(delay)
                    
        logger.info("auto_apply.batch_complete", 
                    success_rate=f"{(success_count/max(1, batch_count))*100:.1f}%",
                    success=success_count,
                    manual_queued=manual_queue_count,
                    total_attempted=batch_count)

    def _classify_job(self, job: Job) -> str:
        url = (job.job_url or "").lower()
        source = (job.source_type or "").lower()
        
        if "workday" in url or "myworkdayjobs" in url:
            return "EXTERNAL"
        elif source in ["greenhouse", "lever", "ashby"]:
            return "FORM_BASED"
        elif source == "ats" or "easy_apply" in source:
            return "EASY_APPLY"
        
        return "EXTERNAL"

    async def _select_resume(self, job: Job) -> Optional[ResumeVersion]:
        # Target based on role category
        role_cat = job.role_category or "Software Engineer"
        stmt = select(ResumeVersion).where(
            and_(
                ResumeVersion.user_id == self.user.id,
                func.lower(ResumeVersion.target_role).contains(role_cat.lower())
            )
        ).order_by(desc(ResumeVersion.created_at)).limit(1)
        res = await self.db.execute(stmt)
        resume = res.scalar_one_or_none()
        
        if not resume:
            # Fallback to primary active
            stmt_fb = select(ResumeVersion).where(
                and_(ResumeVersion.user_id == self.user.id, ResumeVersion.is_active == True)
            ).limit(1)
            res_fb = await self.db.execute(stmt_fb)
            resume = res_fb.scalar_one_or_none()
            
        return resume

    def _get_follow_up_time(self, fit_score: float, now: datetime.datetime) -> datetime.datetime:
        if fit_score >= 80:
            return now + datetime.timedelta(days=2)
        elif fit_score >= 65:
            return now + datetime.timedelta(days=5)
        return now + datetime.timedelta(days=7)

    async def _apply_to_job(self, job: Job, decision: AIDecision) -> ApplicationStatus:
        classification = self._classify_job(job)
        resume = await self._select_resume(job)
        now = datetime.datetime.now(datetime.timezone.utc)
        
        status = ApplicationStatus.SAVED
        notes = f"Classified: {classification} | Resume ID: {resume.id if resume else 'None'}\n"
        
        try:
            if classification == "EASY_APPLY":
                success = await self._execute_direct_api(job, resume)
                if success:
                    status = ApplicationStatus.APPLIED
                    notes += "Direct API apply successful."
                else:
                    status = ApplicationStatus.MANUAL_REQUIRED
                    notes += "Direct API failed. Re-routed to manual."
                    
            elif classification == "FORM_BASED":
                # Extract schema and queue for form processing
                schema_extracted = await self._extract_form_schema(job)
                status = ApplicationStatus.FORM_PENDING
                notes += "Form schema extracted and queued for structured filling."
                
            else: # EXTERNAL
                status = ApplicationStatus.MANUAL_REQUIRED
                notes += "External complex portal. Manual apply required."
                
            follow_up = self._get_follow_up_time(decision.fit_score, now) if status == ApplicationStatus.APPLIED else None
            
            app = Application(
                user_id=self.user.id,
                job_id=job.id,
                resume_version_id=resume.id if resume else None,
                status=status,
                applied_at=now if status == ApplicationStatus.APPLIED else None,
                follow_up_at=follow_up,
                notes=notes,
                platform_used=job.source_type
            )
            
            self.db.add(app)
            await self.db.commit()
            return status
            
        except Exception as e:
            await self.db.rollback()
            logger.error("auto_apply.job_failed", user_id=self.user.id, job_id=job.id, error=str(e))
            return ApplicationStatus.SAVED

    async def _execute_direct_api(self, job: Job, resume: Optional[ResumeVersion]) -> bool:
        """Mock direct Easy Apply execution."""
        logger.debug("auto_apply.execute_direct_api", job_id=job.id)
        await asyncio.sleep(1.0)
        return True
        
    async def _extract_form_schema(self, job: Job) -> bool:
        """Mock extraction of dynamic ATS form schema (Greenhouse/Lever)."""
        logger.debug("auto_apply.extract_form_schema", job_id=job.id)
        await asyncio.sleep(2.0)
        return True
