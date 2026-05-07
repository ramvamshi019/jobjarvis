"""Spam/scam job detector."""
import re
from dataclasses import dataclass, field
from typing import Optional

SPAM_PATTERNS = {
    "no_company_domain": lambda j: not j.get("company_domain"),
    "suspiciously_high_salary": lambda j: (
        (j.get("salary_min") or 0) > 500000 or
        (j.get("salary_max") or 0) > 500000
    ),
    "vague_title": lambda j: bool(re.search(
        r'\b(various|multiple|different|nationwide|various positions)\b',
        j.get("title", ""), re.IGNORECASE
    )),
    "vendor_spam_keywords": lambda j: bool(re.search(
        r'\b(w2 only|c2c|corp[- ]to[- ]corp|no h1|greencard only|gc only'
        r'|direct client|immediate joiners|must be local|local candidates only)\b',
        j.get("description", ""), re.IGNORECASE
    )),
    "recruiter_boilerplate": lambda j: bool(re.search(
        r'\b(we are looking for|we have a requirement|our client is looking'
        r'|kindly share|please revert|send your resume|connect asap)\b',
        j.get("description", ""), re.IGNORECASE
    )),
    "no_description": lambda j: len(j.get("description", "").strip()) < 100,
    "extremely_short_description": lambda j: len(j.get("description", "").strip()) < 50,
    "generic_staffing_title": lambda j: bool(re.search(
        r'\b(resource|consultant required|opening|urgent opening|immediate opening)\b',
        j.get("title", ""), re.IGNORECASE
    )),
}

SPAM_WEIGHTS = {
    "no_description": 0.4,
    "extremely_short_description": 0.5,
    "vendor_spam_keywords": 0.35,
    "recruiter_boilerplate": 0.3,
    "suspiciously_high_salary": 0.25,
    "vague_title": 0.2,
    "generic_staffing_title": 0.3,
    "no_company_domain": 0.1,
}


@dataclass
class SpamResult:
    spam_score: float
    spam_flags: list[str] = field(default_factory=list)
    is_spam: bool = False
    recommendation: str = "allow"


def detect_spam(job: dict) -> SpamResult:
    flags = []
    score = 0.0

    for name, check_fn in SPAM_PATTERNS.items():
        try:
            if check_fn(job):
                flags.append(name)
                score += SPAM_WEIGHTS.get(name, 0.2)
        except Exception:
            pass

    score = min(score, 1.0)
    is_spam = score >= 0.6

    if score >= 0.8:
        recommendation = "reject"
    elif score >= 0.6:
        recommendation = "review"
    elif score >= 0.3:
        recommendation = "flag"
    else:
        recommendation = "allow"

    return SpamResult(
        spam_score=round(score, 3),
        spam_flags=flags,
        is_spam=is_spam,
        recommendation=recommendation,
    )
