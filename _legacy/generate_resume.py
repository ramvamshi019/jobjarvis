"""
AI Resume & Cover Letter Generator.

Features:
  - OpenAI GPT-powered resume tailoring
  - Intelligent retry with exponential backoff
  - Response validation (rejects empty/bad outputs)
  - In-memory caching to avoid re-generating for same job
  - Configurable temperature and token limits
  - Structured logging
"""

import os
import hashlib
import logging
import re
import time
from functools import lru_cache
from typing import Optional

from openai import OpenAI, APIError, RateLimitError, APITimeoutError

from config import (
    OPENAI_API_KEY,
    OPENAI_MODEL,
    OPENAI_TEMPERATURE_RESUME,
    OPENAI_TEMPERATURE_COVER,
    OPENAI_MAX_TOKENS_RESUME,
    OPENAI_MAX_TOKENS_COVER,
    MASTER_RESUME_PATH,
    MAX_RETRIES,
)

logger = logging.getLogger(__name__)

_client: Optional[OpenAI] = None
_generation_cache: dict[str, str] = {}


# ─── Client Management ─────────────────────────────────────────

def get_client() -> OpenAI:
    """Lazy-initialize and return the OpenAI client."""
    global _client
    if _client is None:
        if not OPENAI_API_KEY:
            raise ValueError(
                "OPENAI_API_KEY not set. Add it to your .env file. "
                "Get a key at https://platform.openai.com/api-keys"
            )
        _client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info(f"OpenAI client initialized (model: {OPENAI_MODEL})")
    return _client


# ─── Helpers ────────────────────────────────────────────────────

def load_prompt_template(template_name: str) -> str:
    """Load a prompt template from the prompts directory."""
    path = os.path.join(os.path.dirname(__file__), "prompts", template_name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Prompt template not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_master_resume() -> str:
    """Load the user's master resume."""
    if not os.path.exists(MASTER_RESUME_PATH):
        raise FileNotFoundError(
            f"Master resume not found at {MASTER_RESUME_PATH}. "
            "Create data/master_resume.txt with your full resume text."
        )
    with open(MASTER_RESUME_PATH, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if len(content) < 100:
        raise ValueError("Master resume appears too short. Please add your full resume.")
    return content


def _brace_substitute(template: str, mapping: dict[str, str]) -> str:
    """Replace {key} placeholders without interpreting braces inside values."""
    out = template
    for key, val in mapping.items():
        out = out.replace("{" + key + "}", val)
    return out


def strip_html(text: str) -> str:
    """Remove HTML tags and normalize whitespace."""
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def _cache_key(prefix: str, title: str, company: str) -> str:
    """Generate a cache key for deduplicating generation requests."""
    raw = f"{prefix}:{title}:{company}"
    return hashlib.md5(raw.encode()).hexdigest()


def _validate_output(text: str, min_length: int = 200, context: str = "output") -> str:
    """Validate AI output meets minimum quality standards."""
    if not text or len(text.strip()) < min_length:
        raise ValueError(
            f"Generated {context} is too short ({len(text.strip()) if text else 0} chars). "
            "This may indicate an API issue."
        )

    # Check for common failure patterns
    failure_indicators = [
        "I cannot", "I'm unable", "I don't have", "As an AI",
        "I'm sorry", "ERROR", "UNAUTHORIZED",
    ]
    for indicator in failure_indicators:
        if indicator.lower() in text[:200].lower():
            raise ValueError(f"Generated {context} contains error pattern: '{indicator}'")

    return text.strip()


# ─── AI Generation with Retry ──────────────────────────────────

def _call_openai_with_retry(
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    retries: int = MAX_RETRIES,
) -> str:
    """Call OpenAI API with exponential backoff retry for transient errors."""
    ai = get_client()

    for attempt in range(retries):
        try:
            response = ai.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )

            content = response.choices[0].message.content
            if not content:
                raise ValueError("Empty response from OpenAI")

            # Log usage stats
            usage = response.usage
            if usage:
                logger.debug(
                    f"OpenAI usage: {usage.prompt_tokens} prompt + "
                    f"{usage.completion_tokens} completion = {usage.total_tokens} total tokens"
                )

            return content.strip()

        except RateLimitError as e:
            wait = (2 ** (attempt + 1)) + (attempt * 0.5)
            logger.warning(f"Rate limited (attempt {attempt + 1}/{retries}), waiting {wait:.1f}s: {e}")
            time.sleep(wait)

        except APITimeoutError as e:
            wait = 2 ** attempt
            logger.warning(f"API timeout (attempt {attempt + 1}/{retries}), retrying in {wait}s: {e}")
            time.sleep(wait)

        except APIError as e:
            if e.status_code and e.status_code >= 500:
                wait = 2 ** attempt
                logger.warning(f"Server error (attempt {attempt + 1}/{retries}), retrying in {wait}s: {e}")
                time.sleep(wait)
            else:
                logger.error(f"OpenAI API error (non-retryable): {e}")
                raise

    raise RuntimeError(f"All {retries} OpenAI API retries exhausted")


def _placeholder_tailored_resume(
    master_resume: str,
    job_description: str,
    *,
    company: str = "",
    job_title: str = "",
) -> str:
    """Deterministic output when no AI provider is configured (no API calls)."""
    jd = strip_html(job_description)[:4000]
    lines = [
        "[RESUME PLACEHOLDER — set OPENAI_API_KEY in .env for AI-tailored output]",
        "",
    ]
    if job_title or company:
        lines.append(f"Target: {job_title or '—'} @ {company or '—'}")
        lines.append("")
    lines.extend(
        [
            "=== MASTER RESUME (reference; not auto-rewritten) ===",
            "",
            master_resume.strip()[:6000],
            "",
            "--- Job description (trimmed) ---",
            jd,
        ]
    )
    return "\n".join(lines)


def generate_resume(
    master_resume: str,
    job_description: str,
    *,
    company: str = "",
    job_title: str = "",
) -> str:
    """
    Tailor resume text to a job from a master resume + job description.

    When OPENAI_API_KEY is set, calls the configured model via OpenAI.
    Otherwise returns a long-form placeholder (no keys, no network).

    Optional keyword-only ``company`` and ``job_title`` fill the prompt template.
    """
    clean_desc = strip_html(job_description)
    if len(clean_desc) > 8000:
        clean_desc = clean_desc[:8000] + "..."

    if OPENAI_API_KEY:
        prompt_template = load_prompt_template("resume_prompt.txt")
        prompt = _brace_substitute(
            prompt_template,
            {
                "master_resume": master_resume,
                "company": company or "Not specified",
                "job_title": job_title or "Not specified",
                "job_description": clean_desc,
            },
        )
        system_msg = (
            "You are an expert resume writer specializing in ATS optimization for tech roles. "
            "You ONLY use truthful information from the provided master resume. "
            "You never fabricate experience, skills, certifications, or employment dates."
        )
        text = _call_openai_with_retry(
            system_prompt=system_msg,
            user_prompt=prompt,
            temperature=OPENAI_TEMPERATURE_RESUME,
            max_tokens=OPENAI_MAX_TOKENS_RESUME,
        )
        return _validate_output(text, min_length=300, context="resume")

    logger.warning("OPENAI_API_KEY not set — returning placeholder tailored resume (no AI call)")
    return _placeholder_tailored_resume(
        master_resume, clean_desc, company=company, job_title=job_title
    )


# ─── Resume Generation ─────────────────────────────────────────

def generate_tailored_resume(
    job_title: str, company: str, job_description: str
) -> str:
    """
    Generate an ATS-optimized resume tailored to the specific job.

    Returns the resume text (plaintext, ready for PDF conversion).
    """
    # Check cache
    key = _cache_key("resume", job_title, company)
    if key in _generation_cache:
        logger.info(f"Resume cache hit for {company} — {job_title}")
        return _generation_cache[key]

    logger.info(f"Generating resume for: {company} — {job_title}")
    start = time.time()

    master_resume = load_master_resume()
    resume_text = generate_resume(
        master_resume,
        job_description,
        company=company,
        job_title=job_title,
    )
    if OPENAI_API_KEY:
        resume_text = _validate_output(resume_text, min_length=300, context="resume")
    else:
        resume_text = _validate_output(resume_text, min_length=200, context="resume")

    _generation_cache[key] = resume_text

    elapsed = time.time() - start
    logger.info(f"Resume generated in {elapsed:.1f}s: {len(resume_text)} chars for {company}")
    return resume_text


# ─── Cover Letter Generation ───────────────────────────────────

def generate_cover_letter(
    job_title: str, company: str, job_description: str
) -> str:
    """
    Generate a tailored cover letter for the specific job.

    Returns the cover letter text.
    """
    # Check cache
    key = _cache_key("cover", job_title, company)
    if key in _generation_cache:
        logger.info(f"Cover letter cache hit for {company} — {job_title}")
        return _generation_cache[key]

    logger.info(f"Generating cover letter for: {company} — {job_title}")
    start = time.time()

    master_resume = load_master_resume()
    prompt_template = load_prompt_template("cover_letter_prompt.txt")

    clean_desc = strip_html(job_description)
    if len(clean_desc) > 4000:
        clean_desc = clean_desc[:4000] + "..."

    prompt = _brace_substitute(
        prompt_template,
        {
            "master_resume": master_resume,
            "company": company,
            "job_title": job_title,
            "job_description": clean_desc,
        },
    )

    system_msg = (
        "You are an expert cover letter writer for tech professionals. "
        "You write concise, compelling letters that demonstrate genuine interest. "
        "You ONLY use truthful information from the provided master resume."
    )

    letter = _call_openai_with_retry(
        system_prompt=system_msg,
        user_prompt=prompt,
        temperature=OPENAI_TEMPERATURE_COVER,
        max_tokens=OPENAI_MAX_TOKENS_COVER,
    )

    letter = _validate_output(letter, min_length=150, context="cover letter")

    # Cache the result
    _generation_cache[key] = letter

    elapsed = time.time() - start
    logger.info(f"Cover letter generated in {elapsed:.1f}s: {len(letter)} chars for {company}")
    return letter


# ─── Utility ───────────────────────────────────────────────────

def clear_cache():
    """Clear the generation cache."""
    _generation_cache.clear()
    logger.info("Generation cache cleared")


def get_cache_stats() -> dict:
    """Return cache statistics."""
    return {
        "cached_items": len(_generation_cache),
        "cache_keys": list(_generation_cache.keys()),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sample_desc = """
    We're looking for a Senior Data Engineer to build and maintain our data platform.
    Requirements: Python, SQL, Apache Spark, Airflow, AWS (S3, Glue, Redshift).
    Experience with data modeling, ETL pipelines, and data quality frameworks.
    5+ years of experience. Remote. Competitive salary + equity.
    """
    try:
        resume = generate_tailored_resume("Senior Data Engineer", "TestCo", sample_desc)
        print("=== GENERATED RESUME ===")
        print(resume[:500])
        print(f"\n[Total length: {len(resume)} chars]")
    except Exception as e:
        print(f"Error: {e}")
