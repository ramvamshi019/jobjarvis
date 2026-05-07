"""Classify job source: direct company vs staffing vs consulting vendor."""
import re
from dataclasses import dataclass

STAFFING_KEYWORDS = re.compile(
    r'\b(staffing|recruiter|recruiting firm|talent acquisition firm|workforce|'
    r'manpower|heidrick|kforce|infosys bpo|wipro|tata consultancy|cognizant|'
    r'accenture solutions|capgemini|atos|unison|syntel|igate|mastech|tek systems|'
    r'robert half|dice|ziprecruiter|indeed staffing)\b',
    re.IGNORECASE
)

VENDOR_KEYWORDS = re.compile(
    r'\b(c2c|corp.to.corp|contract.to.hire|w2.only|no.h1|1099|our.client|'
    r'end.client|client.requirement|immediate.opening|resource.needed)\b',
    re.IGNORECASE
)

CONSULTING_KEYWORDS = re.compile(
    r'\b(consulting|consultancy|professional services|services firm|IT services)\b',
    re.IGNORECASE
)

DIRECT_SIGNALS = re.compile(
    r'\b(we are|our team|join us|at {company}|our mission|our culture|our product|'
    r'our platform|we build|we\'re building)\b',
    re.IGNORECASE
)


@dataclass
class SourceClassification:
    source_type: str   # DIRECT_COMPANY|STAFFING_AGENCY|CONSULTING_VENDOR|UNKNOWN
    confidence: float
    reason: str


def classify_source(
    company_name: str,
    description: str,
    domain: str = "",
    ats_type: str = "",
) -> SourceClassification:
    desc = f"{company_name} {description[:500]}"

    if VENDOR_KEYWORDS.search(desc):
        return SourceClassification("STAFFING_AGENCY", 0.90, "vendor/C2C keywords in description")

    if STAFFING_KEYWORDS.search(company_name):
        return SourceClassification("STAFFING_AGENCY", 0.85, "staffing firm in company name")

    if CONSULTING_KEYWORDS.search(company_name) and not DIRECT_SIGNALS.search(description[:300]):
        return SourceClassification("CONSULTING_VENDOR", 0.70, "consulting firm name without direct-hire signals")

    if DIRECT_SIGNALS.search(description[:300]):
        return SourceClassification("DIRECT_COMPANY", 0.80, "direct hiring language in description")

    # ATS type as signal
    if ats_type in ("greenhouse", "lever", "ashby"):
        return SourceClassification("DIRECT_COMPANY", 0.65, f"uses {ats_type} (commonly direct companies)")

    return SourceClassification("UNKNOWN", 0.50, "insufficient signals")
