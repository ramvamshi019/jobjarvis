"""
Application Copilot — helps fill applications WITHOUT auto-submitting.
CRITICAL: Never submit applications automatically. User must approve.
"""
from dataclasses import dataclass, field
from typing import Optional
import structlog

logger = structlog.get_logger(__name__)

COMMON_QUESTIONS = {
    "tell_me_about_yourself": "Professional summary question",
    "why_this_company": "Company-specific motivation question",
    "salary_expectation": "Compensation expectation",
    "relocation": "Relocation willingness",
    "work_authorization": "Work authorization status",
    "availability": "Start date / availability",
    "project_experience": "Specific project or technical question",
    "leadership": "Leadership example",
    "conflict": "Conflict resolution example",
    "strengths": "Professional strengths",
    "weaknesses": "Areas for improvement",
}

RISKY_QUESTIONS = {
    "work_authorization",
    "relocation",
    "salary_expectation",
}


@dataclass
class ApplicationField:
    field_name: str
    question_text: str
    question_type: str
    required: bool = True
    is_risky: bool = False
    suggested_answer: Optional[str] = None
    saved_answer: Optional[str] = None
    warning: Optional[str] = None


@dataclass
class ApplicationSurvey:
    job_id: int
    company_name: str
    job_title: str
    fields: list[ApplicationField] = field(default_factory=list)
    risky_fields: list[str] = field(default_factory=list)
    ready_to_apply: bool = False
    missing_required: list[str] = field(default_factory=list)


def analyze_application(
    job: dict,
    saved_answers: list[dict],
    user_profile: dict,
) -> ApplicationSurvey:
    """
    Analyze a job application and prepare answers.
    Returns a survey with suggested answers — USER MUST REVIEW AND APPROVE.
    """
    saved_map = {a["question_type"]: a["answer_text"] for a in saved_answers}

    fields = []
    risky = []
    missing = []

    # Standard application questions
    for q_type, q_desc in COMMON_QUESTIONS.items():
        is_risky = q_type in RISKY_QUESTIONS
        saved = saved_map.get(q_type)

        if not saved and q_type in ("work_authorization", "availability"):
            missing.append(q_type)

        warning = None
        if is_risky:
            warning = f"⚠️ Review carefully before submitting: {q_desc}"
            risky.append(q_type)

        fields.append(ApplicationField(
            field_name=q_type,
            question_text=q_desc,
            question_type=q_type,
            required=q_type in ("work_authorization", "tell_me_about_yourself"),
            is_risky=is_risky,
            suggested_answer=_generate_suggestion(q_type, job, user_profile),
            saved_answer=saved,
            warning=warning,
        ))

    survey = ApplicationSurvey(
        job_id=job.get("id", 0),
        company_name=job.get("company_name", ""),
        job_title=job.get("title", ""),
        fields=fields,
        risky_fields=risky,
        ready_to_apply=len(missing) == 0,
        missing_required=missing,
    )
    return survey


def _generate_suggestion(q_type: str, job: dict, user_profile: dict) -> str:
    name = user_profile.get("full_name", "the candidate")
    role = job.get("role_category", "Data/AI Engineer")
    company = job.get("company_name", "your company")

    suggestions = {
        "tell_me_about_yourself": f"I'm a {role} with a strong background in building scalable data and AI systems. I specialize in Python, cloud infrastructure, and ML pipelines. I'm passionate about solving complex data problems and have experience delivering production systems end-to-end.",
        "why_this_company": f"I'm drawn to {company} because of its innovative approach to [specific reason]. The {job.get('title', 'role')} aligns perfectly with my expertise in {role.lower()}, and I'm excited about the opportunity to contribute to your team.",
        "salary_expectation": "[Review based on market research and your requirements]",
        "relocation": "[Specify your relocation preferences]",
        "work_authorization": "[State your actual work authorization status accurately]",
        "availability": "I am available to start within 2-4 weeks of an offer.",
        "strengths": "My key strengths are technical depth in data engineering, strong problem-solving, and the ability to communicate complex technical concepts to non-technical stakeholders.",
        "weaknesses": "I sometimes spend too much time optimizing for edge cases. I've learned to balance perfectionism with shipping working solutions iteratively.",
    }
    return suggestions.get(q_type, f"[Customize answer for: {q_type}]")
