"""Resume parser — supports PDF, DOCX, and plain text."""
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import structlog

logger = structlog.get_logger(__name__)

# Skills catalog (reuse from extractor)
from app.ai.skill_extractor import SKILL_CATALOG


@dataclass
class ParsedResume:
    raw_text: str = ""
    skills: dict = field(default_factory=lambda: {"all": [], "required": [], "preferred": []})
    tools: list[str] = field(default_factory=list)
    cloud_platforms: list[str] = field(default_factory=list)
    experience: list[dict] = field(default_factory=list)
    projects: list[dict] = field(default_factory=list)
    education: list[dict] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)
    experience_level: str = "mid"
    years_of_experience: Optional[float] = None
    roles_held: list[str] = field(default_factory=list)
    email: Optional[str] = None
    overall_strength_score: float = 0.0


def parse_pdf(file_bytes: bytes) -> str:
    try:
        from pdfminer.high_level import extract_text
        return extract_text(io.BytesIO(file_bytes)) or ""
    except Exception as e:
        logger.warning("pdf_parse_error", error=str(e))
        return ""


def parse_docx(file_bytes: bytes) -> str:
    try:
        from docx import Document
        doc = Document(io.BytesIO(file_bytes))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as e:
        logger.warning("docx_parse_error", error=str(e))
        return ""


def extract_text_from_resume(file_bytes: bytes, file_type: str) -> str:
    if file_type in ("pdf",):
        return parse_pdf(file_bytes)
    elif file_type in ("docx", "doc"):
        return parse_docx(file_bytes)
    else:
        return file_bytes.decode("utf-8", errors="ignore")


# Patterns for structured extraction
_EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
_YEARS_EXP_PATTERN = re.compile(r'(\d+\.?\d*)\+?\s+years?\s+(?:of\s+)?(?:experience|exp)', re.IGNORECASE)
_CERT_PATTERNS = re.compile(
    r'\b(aws\s+certified|gcp\s+certified|azure\s+certified|cka\b|ckad\b|dbt\s+certified'
    r'|databricks\s+certified|snowflake\s+certified|tensorflow\s+cert|pmp\b|csm\b'
    r'|machine\s+learning\s+engineer\s+cert)\b',
    re.IGNORECASE
)
_SECTION_HEADERS = re.compile(
    r'^(experience|education|skills|projects|certifications|summary|objective'
    r'|work\s+history|technical\s+skills|professional\s+experience)\s*:?\s*$',
    re.IGNORECASE | re.MULTILINE
)


def parse_resume(text: str) -> ParsedResume:
    """Parse resume text into structured profile."""
    if not text:
        return ParsedResume()

    # Email (masked in production but used for identity)
    email_m = _EMAIL_PATTERN.search(text)
    email = email_m.group(0) if email_m else None

    # Years of experience
    years = None
    years_m = _YEARS_EXP_PATTERN.search(text)
    if years_m:
        years = float(years_m.group(1))

    # Skills
    all_skills = []
    tools = []
    cloud = []
    for skill, patterns in SKILL_CATALOG.items():
        for p in patterns:
            if re.search(p, text, re.IGNORECASE):
                all_skills.append(skill)
                if skill in ("AWS", "Azure", "GCP"):
                    cloud.append(skill)
                else:
                    tools.append(skill)
                break

    # Certifications
    certs = list(set(CERT_PATTERNS.findall(text) if hasattr(CERT_PATTERNS, 'findall') else
                     re.findall(_CERT_PATTERNS.pattern, text, re.IGNORECASE)))

    # Experience level
    exp_level = "mid"
    if years is not None:
        if years < 2:
            exp_level = "entry"
        elif years >= 7:
            exp_level = "senior"
        elif years >= 4:
            exp_level = "mid"

    # Detect senior titles
    if re.search(r'\b(senior|staff|principal|lead|architect|director)\b', text, re.IGNORECASE):
        exp_level = "senior"

    # Overall strength score (heuristic)
    strength = min(100.0, len(all_skills) * 3.5 + (years or 3) * 4 + len(certs) * 8)

    return ParsedResume(
        raw_text=text,
        skills={"all": all_skills, "required": [], "preferred": []},
        tools=tools,
        cloud_platforms=cloud,
        certifications=certs,
        experience_level=exp_level,
        years_of_experience=years,
        overall_strength_score=min(100.0, strength),
    )


CERT_PATTERNS = _CERT_PATTERNS

def parse_resume_from_bytes(file_bytes: bytes, file_type: str) -> ParsedResume:
    text = extract_text_from_resume(file_bytes, file_type)
    return parse_resume(text)
