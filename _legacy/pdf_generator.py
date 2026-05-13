"""
Professional PDF Generator — creates ATS-friendly resume and cover letter PDFs.

Features:
  - Clean, professional single-column layout
  - ATS-optimized formatting (no tables, images, or complex layouts)
  - Smart section detection (headers, bullets, contact info, dates)
  - Proper page margins and typography
  - Cover letter with formal business letter formatting
"""

import os
import logging
import re
from xml.sax.saxutils import escape as _xml_escape
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    HRFlowable,
)

from config import RESUME_OUTPUT_DIR

logger = logging.getLogger(__name__)


def _rl_paragraph_escape(text: str) -> str:
    """Escape text for ReportLab Paragraph (XML-like markup)."""
    if not text:
        return ""
    return _xml_escape(str(text), entities={'"': "&quot;", "'": "&apos;"})


# ─── Color Palette ──────────────────────────────────────────────

COLORS = {
    "primary": HexColor("#1a1a2e"),      # Deep navy for headers
    "secondary": HexColor("#2d2d2d"),     # Dark gray for titles
    "body": HexColor("#333333"),          # Body text
    "meta": HexColor("#555555"),          # Metadata (dates, etc.)
    "contact": HexColor("#444444"),       # Contact info
    "divider": HexColor("#c0c0c0"),       # Section dividers
    "accent": HexColor("#2563eb"),        # Blue accent for links
}


# ─── Styles ─────────────────────────────────────────────────────

def get_resume_styles():
    """Create professional ATS-friendly resume styles."""
    styles = getSampleStyleSheet()

    styles.add(ParagraphStyle(
        "ResumeName",
        parent=styles["Heading1"],
        fontSize=20,
        leading=24,
        alignment=TA_CENTER,
        spaceAfter=2,
        textColor=COLORS["primary"],
        fontName="Helvetica-Bold",
    ))

    styles.add(ParagraphStyle(
        "ResumeContact",
        parent=styles["Normal"],
        fontSize=9.5,
        leading=13,
        alignment=TA_CENTER,
        spaceAfter=10,
        textColor=COLORS["contact"],
        fontName="Helvetica",
    ))

    styles.add(ParagraphStyle(
        "SectionHeader",
        parent=styles["Heading2"],
        fontSize=11.5,
        leading=15,
        spaceBefore=12,
        spaceAfter=5,
        textColor=COLORS["primary"],
        fontName="Helvetica-Bold",
        borderWidth=0,
    ))

    styles.add(ParagraphStyle(
        "JobTitle",
        parent=styles["Normal"],
        fontSize=10.5,
        leading=13,
        fontName="Helvetica-Bold",
        spaceAfter=1,
        textColor=COLORS["secondary"],
    ))

    styles.add(ParagraphStyle(
        "JobMeta",
        parent=styles["Normal"],
        fontSize=9.5,
        leading=12,
        fontName="Helvetica-Oblique",
        spaceAfter=4,
        textColor=COLORS["meta"],
    ))

    styles.add(ParagraphStyle(
        "BulletText",
        parent=styles["Normal"],
        fontSize=9.5,
        leading=12.5,
        leftIndent=16,
        firstLineIndent=-10,
        spaceAfter=2,
        alignment=TA_JUSTIFY,
        textColor=COLORS["body"],
        fontName="Helvetica",
    ))

    styles.add(ParagraphStyle(
        "NormalText",
        parent=styles["Normal"],
        fontSize=9.5,
        leading=12.5,
        spaceAfter=3,
        alignment=TA_JUSTIFY,
        textColor=COLORS["body"],
        fontName="Helvetica",
    ))

    styles.add(ParagraphStyle(
        "SkillsText",
        parent=styles["Normal"],
        fontSize=9.5,
        leading=13,
        spaceAfter=3,
        textColor=COLORS["body"],
        fontName="Helvetica",
    ))

    return styles


# ─── Resume Parser ──────────────────────────────────────────────

# Common section headers to detect
SECTION_HEADERS = {
    "professional summary", "summary", "profile", "objective",
    "experience", "work experience", "professional experience", "employment",
    "education", "academic background",
    "skills", "technical skills", "core competencies", "technologies",
    "projects", "key projects", "notable projects",
    "certifications", "certificates", "licenses",
    "awards", "honors", "achievements",
    "publications", "patents",
    "volunteer", "leadership",
}


def _is_section_header(line: str) -> bool:
    """Detect if a line is a section header."""
    clean = re.sub(r"[=\-_*#|:]+", "", line).strip()
    if not clean:
        return False
    if clean.lower() in SECTION_HEADERS:
        return True
    if clean.isupper() and 3 < len(clean) < 45 and not clean.startswith(("•", "-", "–")):
        return True
    return False


def _is_bullet(line: str) -> bool:
    """Detect if a line is a bullet point."""
    return bool(re.match(r"^\s*[•\-–▪*►◆→]\s+", line))


def _is_contact_line(line: str) -> bool:
    """Detect if a line looks like contact information."""
    indicators = ["@", "|", "linkedin", "github", "phone", "tel:", "http"]
    return any(ind in line.lower() for ind in indicators)


def _is_date_line(line: str) -> bool:
    """Detect lines with date ranges (e.g., 'Jan 2021 – Present')."""
    return bool(re.search(
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|20\d{2}|present|current)",
        line, re.IGNORECASE
    )) and bool(re.search(r"[–\-—to]", line))


def parse_resume_to_flowables(resume_text: str, styles) -> list:
    """Parse plaintext resume into ReportLab flowable elements."""
    flowables = []
    lines = resume_text.split("\n")

    # Track state for smart formatting
    found_name = False
    found_contact = False

    for i, raw_line in enumerate(lines):
        line = raw_line.strip()

        if not line:
            flowables.append(Spacer(1, 3))
            continue

        # ── Name (first non-empty, non-bullet line < 60 chars) ──
        if not found_name and i < 5 and len(line) < 60 and not _is_bullet(line):
            flowables.append(Paragraph(_rl_paragraph_escape(line), styles["ResumeName"]))
            found_name = True
            continue

        # ── Contact info (early line with @ or |) ──
        if not found_contact and i < 6 and _is_contact_line(line):
            flowables.append(Paragraph(_rl_paragraph_escape(line), styles["ResumeContact"]))
            found_contact = True
            continue

        # ── Section headers ──
        if _is_section_header(line):
            clean = re.sub(r"[=\-_*#|:]+", "", line).strip()
            flowables.append(Spacer(1, 4))
            flowables.append(HRFlowable(
                width="100%", thickness=0.6,
                color=COLORS["divider"], spaceAfter=3,
            ))
            flowables.append(Paragraph(_rl_paragraph_escape(clean.upper()), styles["SectionHeader"]))
            continue

        # ── Bullet points ──
        if _is_bullet(line):
            bullet_text = re.sub(r"^\s*[•\-–▪*►◆→]\s+", "", line).strip()
            if bullet_text:
                bullet_text = _rl_paragraph_escape(bullet_text)
                flowables.append(Paragraph(f"• {bullet_text}", styles["BulletText"]))
            continue

        # ── Job title lines (bold, often with company) ──
        if re.match(r"^[A-Z].*\|.*\|", line) or re.match(r"^[A-Z].*,.*\d{4}", line):
            flowables.append(Paragraph(_rl_paragraph_escape(line), styles["JobTitle"]))
            continue

        # ── Date ranges / metadata lines ──
        if _is_date_line(line) and len(line) < 80:
            flowables.append(Paragraph(_rl_paragraph_escape(line), styles["JobMeta"]))
            continue

        # ── Skills lines (comma-separated lists) ──
        if line.count(",") >= 3 and len(line) > 30:
            flowables.append(Paragraph(_rl_paragraph_escape(line), styles["SkillsText"]))
            continue

        # ── Default: normal paragraph ──
        flowables.append(Paragraph(_rl_paragraph_escape(line), styles["NormalText"]))

    return flowables


# ─── PDF Generation ─────────────────────────────────────────────

def generate_pdf(resume_text: str, job_id: str, filename: str = None) -> str:
    """
    Generate an ATS-friendly PDF from resume text.
    Returns the path to the generated PDF file.
    """
    os.makedirs(RESUME_OUTPUT_DIR, exist_ok=True)

    if filename is None:
        filename = f"resume_{job_id}.pdf"

    filepath = os.path.join(RESUME_OUTPUT_DIR, filename)
    styles = get_resume_styles()
    flowables = parse_resume_to_flowables(resume_text, styles)

    if not flowables:
        raise ValueError("No content to generate PDF from")

    doc = SimpleDocTemplate(
        filepath,
        pagesize=letter,
        leftMargin=0.65 * inch,
        rightMargin=0.65 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
        title=f"Resume - {job_id}",
        author="Job Automation System",
    )

    try:
        doc.build(flowables)
        file_size = os.path.getsize(filepath)
        logger.info(f"Resume PDF generated: {filepath} ({file_size / 1024:.1f} KB)")
    except Exception as e:
        logger.error(f"Resume PDF generation failed for {job_id}: {e}")
        raise

    return filepath


def generate_cover_letter_pdf(cover_letter_text: str, job_id: str) -> str:
    """Generate a professional cover letter PDF."""
    os.makedirs(RESUME_OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(RESUME_OUTPUT_DIR, f"cover_letter_{job_id}.pdf")

    styles = get_resume_styles()

    # Cover letter uses wider margins (business letter format)
    doc = SimpleDocTemplate(
        filepath,
        pagesize=letter,
        leftMargin=1 * inch,
        rightMargin=1 * inch,
        topMargin=1 * inch,
        bottomMargin=1 * inch,
        title=f"Cover Letter - {job_id}",
        author="Job Automation System",
    )

    flowables = []

    # Parse cover letter into paragraphs
    paragraphs = cover_letter_text.split("\n\n")
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # Handle multi-line paragraphs within a block
        lines = para.split("\n")
        combined = " ".join(line.strip() for line in lines if line.strip())
        if combined:
            # Escape XML characters
            combined = _rl_paragraph_escape(combined)
            flowables.append(Paragraph(combined, styles["NormalText"]))
            flowables.append(Spacer(1, 8))

    if not flowables:
        raise ValueError("No content for cover letter PDF")

    try:
        doc.build(flowables)
        file_size = os.path.getsize(filepath)
        logger.info(f"Cover letter PDF generated: {filepath} ({file_size / 1024:.1f} KB)")
    except Exception as e:
        logger.error(f"Cover letter PDF failed for {job_id}: {e}")
        raise

    return filepath


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sample = """John Doe
john.doe@email.com | (555) 123-4567 | linkedin.com/in/johndoe | github.com/johndoe

PROFESSIONAL SUMMARY
Experienced Data Engineer with 6+ years building scalable data pipelines and platforms.
Proficient in Python, SQL, Apache Spark, Apache Airflow, and cloud-native architectures on AWS and GCP.

EXPERIENCE
Senior Data Engineer | TechCorp Inc. | Jan 2021 – Present
• Architected real-time data pipeline using Apache Kafka and Spark Streaming, processing 50M+ events/day with 99.9% uptime
• Reduced ETL job runtime by 40% through query optimization and partitioning strategies on Snowflake
• Led migration of on-premise data warehouse to Snowflake, cutting infrastructure costs by $200K/year
• Mentored 3 junior engineers and established team coding standards and code review processes

Data Engineer | DataFlow LLC | Mar 2018 – Dec 2020
• Built and maintained 30+ Apache Airflow DAGs for daily batch ETL processing across 15 data sources
• Implemented data quality framework using Great Expectations, reducing downstream errors by 85%
• Designed star schema data model supporting 500+ daily analytical queries with <2s average response time

TECHNICAL SKILLS
Languages: Python, SQL, Java, Scala
Data: Apache Spark, Apache Airflow, Apache Kafka, dbt, Snowflake, Redshift, BigQuery
Cloud: AWS (S3, Glue, EMR, Lambda, Redshift), GCP (BigQuery, Dataflow, Cloud Functions)
Infrastructure: Docker, Kubernetes, Terraform, CI/CD, Git, Linux

EDUCATION
B.S. Computer Science | State University | 2018
"""
    path = generate_pdf(sample, "test_001")
    print(f"Generated: {path}")
