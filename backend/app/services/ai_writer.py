"""
AI text-generation service for job-application assistance.

Two features:
  • generate_cover_letter(resume, job, user)  → custom cover letter
  • tailor_resume(resume, job)                → resume rewritten for the job

Provider selection (set via env var AI_PROVIDER):
  anthropic  — Claude (recommended, best at long-form writing)
  openai     — GPT-4o-mini (cheap, fast, good enough)

Falls back to a template-based generator if neither API key is set,
so the feature works for free if the user can't afford an API key.
"""
from __future__ import annotations

import os
import logging
import textwrap
from typing import Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
#  Provider abstraction
# ──────────────────────────────────────────────────────────────────────────────

def _provider() -> str:
    """Pick the best available provider based on env vars."""
    explicit = (os.environ.get("AI_PROVIDER") or "").lower().strip()
    if explicit in ("anthropic", "openai", "template"):
        return explicit
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "template"


def _call_anthropic(system: str, user: str, max_tokens: int = 800) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    # Default to Claude Haiku 4.5 — cheap, fast, plenty good for cover-letter / Q&A.
    # Override via env var ANTHROPIC_MODEL if you want Sonnet/Opus.
    model = os.environ.get("ANTHROPIC_MODEL") or "claude-haiku-4-5-20251001"
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return msg.content[0].text
    except Exception as e:
        logger.error("anthropic_call_failed model=%s err=%s", model, e)
        raise


def _call_openai(system: str, user: str, max_tokens: int = 800) -> str:
    import openai
    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content or ""


# ── Budget enforcement ────────────────────────────────────────────────────
# Estimates cost per call before hitting the provider and tracks daily spend
# in Redis.  Hits the kill-switch at AI_DAILY_BUDGET_USD.  This is a soft
# cap (refused calls fall through to template), not a hard one — Anthropic /
# OpenAI also let you set spend limits in their consoles for belt-and-braces.

# Rough $/MTok prices.  We over-estimate slightly so we err on the side of
# refusing rather than over-spending.
_PRICE_PER_MTOK = {
    # Anthropic (input, output)
    "claude-haiku-4-5":   (1.00, 5.00),
    "claude-sonnet-4-6":  (3.00, 15.00),
    "claude-opus-4-6":    (15.00, 75.00),
    # OpenAI
    "gpt-4o-mini":        (0.15, 0.60),
    "gpt-4o":             (2.50, 10.00),
}


def _estimate_cost_usd(model: str, in_tokens: int, max_out_tokens: int) -> float:
    # Find the closest price entry by prefix
    key = next(
        (k for k in _PRICE_PER_MTOK if model.startswith(k)),
        "claude-haiku-4-5",  # default to haiku pricing
    )
    in_price, out_price = _PRICE_PER_MTOK[key]
    return (in_tokens * in_price + max_out_tokens * out_price) / 1_000_000


def _redis_client():
    """Lazy redis import so the module imports clean when redis isn't installed."""
    try:
        import redis
        return redis.from_url(
            os.environ.get("REDIS_URL", "redis://redis:6379/0"),
            decode_responses=True,
        )
    except Exception:
        return None


def _budget_check(estimated_cost: float) -> bool:
    """
    Return True if the estimated call fits within the day's remaining budget.
    Falls through (returns True) if Redis isn't reachable so we never block
    AI usage purely due to infrastructure failure.
    """
    from datetime import date as _date
    cap_str = os.environ.get("AI_DAILY_BUDGET_USD", "")
    try:
        cap = float(cap_str)
    except (TypeError, ValueError):
        return True  # no cap set, allow
    if cap <= 0:
        return True

    r = _redis_client()
    if r is None:
        return True  # can't track, don't block

    key = f"ai_spend:{_date.today().isoformat()}"
    try:
        spent_str = r.get(key) or "0"
        spent = float(spent_str)
    except Exception:
        spent = 0.0

    if spent + estimated_cost > cap:
        logger.warning(
            "ai_budget_cap_hit cap=%.2f spent=%.4f would_add=%.4f",
            cap, spent, estimated_cost,
        )
        return False
    return True


def _record_spend(estimated_cost: float) -> None:
    from datetime import date as _date
    r = _redis_client()
    if r is None:
        return
    key = f"ai_spend:{_date.today().isoformat()}"
    try:
        r.incrbyfloat(key, estimated_cost)
        # Expire keys after 48h so they don't accumulate forever.
        r.expire(key, 60 * 60 * 48)
    except Exception:
        pass


def _generate(system: str, user: str, max_tokens: int = 800) -> str:
    provider = _provider()

    # Budget pre-flight: estimate the worst case (full max_tokens reply).
    # Input estimated as char_count/4 — fine for budget purposes.
    in_tokens = (len(system) + len(user)) // 4
    if provider == "anthropic":
        model = os.environ.get("ANTHROPIC_MODEL") or "claude-haiku-4-5-20251001"
    elif provider == "openai":
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    else:
        model = ""
    est_cost = _estimate_cost_usd(model, in_tokens, max_tokens) if model else 0.0

    if model and not _budget_check(est_cost):
        return (
            "[AI daily budget cap reached. Falls back to template until "
            "midnight UTC. Raise AI_DAILY_BUDGET_USD if you need more.]"
        )

    try:
        if provider == "anthropic":
            out = _call_anthropic(system, user, max_tokens)
            _record_spend(est_cost)
            return out
        if provider == "openai":
            out = _call_openai(system, user, max_tokens)
            _record_spend(est_cost)
            return out
    except Exception:
        logger.warning("ai_writer.api_call_failed", exc_info=True)
        # fall through to template
    return _template_fallback(system, user)


def _template_fallback(system: str, user: str) -> str:
    """No API key configured — produce a minimally-useful template."""
    return (
        "[No AI key configured — install Claude or OpenAI to enable real "
        "generation. Set ANTHROPIC_API_KEY or OPENAI_API_KEY in deploy/.env.]"
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Cover letter
# ──────────────────────────────────────────────────────────────────────────────

COVER_LETTER_SYSTEM = textwrap.dedent("""
    You are a career coach helping a job seeker write a concise, compelling
    cover letter. Rules:
      - 3 short paragraphs (200–300 words total).
      - Paragraph 1: name the role + 1-sentence hook that matches your background to it.
      - Paragraph 2: 2–3 concrete achievements from your resume that map onto the
        company's needs.  Use numbers when present (X% improvement, $Y, N users).
      - Paragraph 3: brief, specific reason you want to work at THIS company
        (something from their job description, not generic).
      - First-person, professional but warm. No clichés ("dynamic team player").
      - Plain text, no markdown, no signature line.
""").strip()


def generate_cover_letter(
    *,
    resume_text: str,
    job_title: str,
    company_name: str,
    job_description: str,
    user_full_name: Optional[str] = None,
) -> str:
    """Return a 200-300 word custom cover letter as plain text."""
    user_msg = textwrap.dedent(f"""
        APPLICANT NAME: {user_full_name or '[Name]'}

        ROLE: {job_title}
        COMPANY: {company_name}

        JOB DESCRIPTION:
        {(job_description or '').strip()[:3000]}

        APPLICANT RESUME (raw text):
        {(resume_text or '').strip()[:4000]}

        Write the cover letter now.
    """).strip()
    return _generate(COVER_LETTER_SYSTEM, user_msg, max_tokens=700).strip()


# ──────────────────────────────────────────────────────────────────────────────
#  Resume tailoring
# ──────────────────────────────────────────────────────────────────────────────

TAILOR_SYSTEM = textwrap.dedent("""
    You are an expert ATS-optimization resume writer.  Given a resume and a
    job description, rewrite each bullet point in the resume so that:

      - It uses the exact keywords from the job description where truthful.
      - It quantifies impact (numbers, %, $, scale).
      - Every bullet starts with a strong action verb.
      - You DO NOT invent skills the user doesn't have.  Only re-phrase existing
        content to better surface relevant skills.

    Output ONLY the rewritten resume in plain text, preserving section headers
    and structure.  No commentary.
""").strip()


def tailor_resume(
    *,
    resume_text: str,
    job_title: str,
    company_name: str,
    job_description: str,
) -> str:
    """Return a job-tailored version of the resume as plain text."""
    user_msg = textwrap.dedent(f"""
        TARGET ROLE: {job_title} @ {company_name}

        TARGET JOB DESCRIPTION:
        {(job_description or '').strip()[:3500]}

        ORIGINAL RESUME:
        {(resume_text or '').strip()[:5000]}

        Rewrite the resume now, preserving structure but optimizing for this
        specific role.
    """).strip()
    return _generate(TAILOR_SYSTEM, user_msg, max_tokens=1800).strip()


# ──────────────────────────────────────────────────────────────────────────────
#  Job-fit summary (small, used by the auto-apply pre-flight)
# ──────────────────────────────────────────────────────────────────────────────

FIT_SUMMARY_SYSTEM = textwrap.dedent("""
    You are a hiring manager evaluating fit.  Given a resume and a job,
    output a short summary in JSON form:

    {
      "fit_score":      0-100 integer,
      "top_strengths":  ["...", "...", "..."],  # 3 short bullets
      "top_gaps":       ["...", "..."],          # up to 2 short bullets
      "should_apply":   true | false
    }

    Be strict.  Only output the JSON.
""").strip()


# ──────────────────────────────────────────────────────────────────────────────
#  Custom application-question answering
# ──────────────────────────────────────────────────────────────────────────────

ANSWER_QUESTION_SYSTEM = textwrap.dedent("""
    You are answering an application question on a job-posting form for an
    applicant.  Rules:
      - First-person, present-tense, professional but warm.
      - Cite at least ONE concrete project / role from the resume when possible.
      - Quantify with real numbers if the resume has them.
      - 60-150 words unless the question explicitly asks for one sentence.
      - Don't invent skills the applicant doesn't have.
      - For multiple-choice / yes-no questions, answer with ONLY the option text.
      - For demographic / EEOC questions, output exactly: "Decline to answer".
      - For salary expectations, give a range based on the resume's seniority
        if no other signal is available.
""").strip()


# Hard-coded answers for common form questions — saves API calls + cost
_COMMON_ANSWERS = {
    "preferred pronouns": "Decline to answer",
    "gender":             "Decline to answer",
    "race":               "Decline to answer",
    "ethnicity":          "Decline to answer",
    "veteran":            "Decline to answer",
    "disability":         "Decline to answer",
    "lgbtq":              "Decline to answer",

    # Standard work-auth/sponsorship
    "authorized to work":              "Yes",
    "legally authorized":              "Yes",
    "require sponsorship":             "No",
    "require visa":                    "No",
    "h-1b":                            "No",
    "h1b":                             "No",
    "future sponsorship":              "No",

    # Standard agreements
    "agree to provide original":       "I agree",
    "i agree":                         "I agree",
    "non-compete":                     "No",
    "non compete":                     "No",
    "non-solicit":                     "No",

    # Heard about us
    "how did you hear":                "Through JobJarvis, a personal job-matching tool I built.",
    "how did you find":                "Through JobJarvis, a personal job-matching tool I built.",

    # Misc admin questions
    "mailing address":                 "Available upon request.",
    "current company":                 "Open to new opportunities.",
    "current employer":                "Open to new opportunities.",
    "salary expectations":             "Flexible based on total compensation package and role scope.",
    "expected salary":                 "Flexible based on total compensation package and role scope.",
    "notice period":                   "Two weeks.",
    "start date":                      "Available to start within two weeks of offer acceptance.",
    "availability":                    "Available to start within two weeks of offer acceptance.",
}


def _try_canonical_answer(question: str) -> Optional[str]:
    """Match against the canned-answer table before hitting the AI."""
    q = (question or "").lower()
    for key, answer in _COMMON_ANSWERS.items():
        if key in q:
            return answer
    return None


def answer_application_question(
    *,
    question: str,
    resume_text: str,
    job_title: str = "",
    company_name: str = "",
    job_description: str = "",
) -> str:
    """
    Return a single answer string for an application form question.

    Hits the canned-answer table first; only calls AI for novel questions.
    """
    canon = _try_canonical_answer(question)
    if canon is not None:
        return canon

    user_msg = textwrap.dedent(f"""
        APPLICANT RESUME:
        {(resume_text or '').strip()[:3000]}

        ROLE BEING APPLIED FOR: {job_title} @ {company_name}

        JOB DESCRIPTION (for context):
        {(job_description or '').strip()[:2000]}

        QUESTION TO ANSWER:
        {question.strip()}

        Write the answer now.
    """).strip()
    return _generate(ANSWER_QUESTION_SYSTEM, user_msg, max_tokens=400).strip()


def fit_summary(*, resume_text: str, job_title: str, job_description: str) -> str:
    user_msg = textwrap.dedent(f"""
        ROLE: {job_title}
        JOB DESCRIPTION:
        {(job_description or '').strip()[:2500]}

        RESUME:
        {(resume_text or '').strip()[:3500]}

        Output the JSON now.
    """).strip()
    return _generate(FIT_SUMMARY_SYSTEM, user_msg, max_tokens=400).strip()
