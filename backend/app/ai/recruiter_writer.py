"""Generate recruiter emails, LinkedIn messages, cover letters, and follow-ups."""
from dataclasses import dataclass
from typing import Optional
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class OutreachBundle:
    recruiter_email_subject: str
    recruiter_email_body: str
    linkedin_dm: str
    cover_letter: str
    follow_up_3d: str
    follow_up_7d: str


def generate_outreach(
    job: dict,
    user_profile: dict,
    resume_version: str = "",
    matched_skills: list[str] = None,
    missing_skills: list[str] = None,
) -> OutreachBundle:
    """Generate all outreach materials for a job application."""
    matched_skills = matched_skills or []
    missing_skills = missing_skills or []

    name = user_profile.get("full_name", "Candidate")
    title = job.get("title", "the position")
    company = job.get("company_name", "your company")
    role_cat = job.get("role_category", "Data/AI Engineer")

    top_skills = ", ".join(matched_skills[:4]) if matched_skills else "Python, SQL, cloud technologies"

    # ── Recruiter Email ────────────────────────────────────────────
    email_subject = f"Strong candidate for {title} – {name}"

    email_body = f"""Hi,

I came across the {title} role at {company} and believe I'm an excellent fit for this position.

With my background in {role_cat.lower()} and hands-on experience with {top_skills}, I've built and scaled production data systems that deliver measurable business impact.

Key highlights:
• {len(matched_skills)} direct skill matches including {', '.join(matched_skills[:3])}
• Proven track record in {role_cat} roles
• Ready to contribute immediately

I'd love to connect for a quick 15-minute call to discuss how I can contribute to your team. I've attached my resume for your review.

Best regards,
{name}"""

    # ── LinkedIn DM ────────────────────────────────────────────────
    linkedin_dm = (
        f"Hi! I noticed the {title} opening at {company} and I'm very interested. "
        f"My background in {', '.join(matched_skills[:2] or [role_cat])} aligns well with the role. "
        f"Would love to connect and learn more about the team!"
    )

    # ── Cover Letter ───────────────────────────────────────────────
    cover_letter = f"""Dear Hiring Manager,

I am writing to express my strong interest in the {title} position at {company}. With my experience in {role_cat} and expertise in {top_skills}, I am confident I can make an immediate and lasting contribution to your team.

In my previous roles, I have:
• Designed and implemented production-grade {role_cat.lower()} systems
• Worked extensively with {', '.join(matched_skills[:4] or ['modern data tools'])}
• Collaborated cross-functionally with product, engineering, and data science teams

What excites me most about {company} is the opportunity to work on challenging problems at scale. I am particularly drawn to this role because it aligns with my expertise in {role_cat.lower()} and my passion for building systems that drive real business value.

{f"I am actively developing skills in {', '.join(missing_skills[:2])}, which I believe will further strengthen my contributions." if missing_skills else ""}

I would welcome the opportunity to discuss how my background and skills can help {company} achieve its goals. Thank you for considering my application.

Sincerely,
{name}"""

    # ── Follow-ups ─────────────────────────────────────────────────
    follow_up_3d = f"""Hi,

I wanted to follow up on my application for the {title} role at {company} that I submitted 3 days ago. I remain very enthusiastic about this opportunity and believe my {role_cat} background is a strong fit.

If it would be helpful, I'm happy to share specific examples of my work in {', '.join(matched_skills[:2] or ['this domain'])}.

Looking forward to hearing from you!

Best,
{name}"""

    follow_up_7d = f"""Hi,

I hope you're doing well. I wanted to check in one more time regarding the {title} position at {company}.

I'm still very interested in this opportunity and would love to learn more about the team and the challenges you're solving. Please feel free to reach out at your convenience.

Thank you for your time,
{name}"""

    return OutreachBundle(
        recruiter_email_subject=email_subject,
        recruiter_email_body=email_body,
        linkedin_dm=linkedin_dm,
        cover_letter=cover_letter,
        follow_up_3d=follow_up_3d,
        follow_up_7d=follow_up_7d,
    )
