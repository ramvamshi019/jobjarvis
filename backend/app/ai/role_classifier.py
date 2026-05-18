"""Role classification engine — returns role category, confidence, reason."""
import re
from dataclasses import dataclass
from typing import Optional
import structlog

logger = structlog.get_logger(__name__)

ROLE_PATTERNS: list[tuple[str, list[str]]] = [
    ("AI Engineer", [
        r'\bai\s+engineer\b', r'\bartificial intelligence engineer\b',
        r'\bllm\s+engineer\b', r'\bgenai\b', r'\bgenerative\s+ai\b',
        r'\bprompt\s+engineer\b', r'\bai\s+platform\b', r'\bfoundation\s+model\b',
    ]),
    ("ML Engineer", [
        r'\bml\s+engineer\b', r'\bmachine\s+learning\s+engineer\b',
        r'\bdeep\s+learning\s+engineer\b', r'\bml\s+platform\b',
        r'\bmodel\s+training\b', r'\bmodel\s+deployment\b', r'\bneural\s+network\b',
    ]),
    ("MLOps Engineer", [
        r'\bmlops\b', r'\bml\s+ops\b', r'\bml\s+infrastructure\b',
        r'\bmodel\s+serving\b', r'\bml\s+platform\s+engineer\b',
        r'\bkubeflow\b', r'\bmlflow\b', r'\bvertex\s+ai\b',
    ]),
    ("Data Engineer", [
        r'\bdata\s+engineer\b', r'\bdata\s+pipeline\b',
        r'\betl\b', r'\belt\b', r'\bdata\s+platform\b',
        r'\bpyspark\b', r'\bspark\s+engineer\b', r'\bdata\s+infrastructure\b',
        r'\bdatabricks\b.*engineer', r'\bairflow\b.*engineer',
    ]),
    ("Data Platform Engineer", [
        r'\bdata\s+platform\s+engineer\b', r'\blakehouse\b',
        r'\bdata\s+lake\b.*engineer', r'\bdelta\s+lake\b',
    ]),
    ("Analytics Engineer", [
        r'\banalytics\s+engineer\b', r'\bdbt\s+engineer\b',
        r'\banalytics\s+platform\b', r'\bsql\s+engineer\b',
        r'\bdata\s+analytics\s+engineer\b',
    ]),
    ("Backend Engineer", [
        r'\bbackend\s+engineer\b', r'\bback[\s-]end\s+engineer\b',
        r'\bapi\s+engineer\b', r'\bserver[\s-]side\b',
        r'\bplatform\s+engineer\b', r'\bmicroservices\b.*engineer',
    ]),
    ("QA/SDET", [
        r'\bsdet\b', r'\bquality\s+assurance\b', r'\bqa\s+engineer\b',
        r'\btest\s+automation\b', r'\bautomation\s+engineer\b',
        r'\bsoftware\s+test\b', r'\bperformance\s+test\b',
    ]),
    ("Software Engineer", [
        r'\bsoftware\s+engineer\b', r'\bsoftware\s+developer\b',
        r'\bfull[\s-]?stack\b', r'\bfrontend\s+engineer\b',
    ]),
]

RELEVANT_ROLES = {
    "AI Engineer", "ML Engineer", "Data Engineer", "Data Platform Engineer",
    "MLOps Engineer", "Analytics Engineer", "Backend Engineer", "QA/SDET",
    "Software Engineer",
}

RELEVANT_KEYWORDS = re.compile(
    r'\b(python|spark|pyspark|kafka|airflow|dbt|snowflake|databricks|bigquery|redshift'
    r'|aws|azure|gcp|kubernetes|docker|mlflow|kubeflow|tensorflow|pytorch|scikit'
    r'|sql|data\s+pipeline|machine\s+learning|deep\s+learning|llm|rag|vector'
    r'|fastapi|django|flask|microservice|api|rest|grpc|redis|celery|etl|elt'
    r'|sdet|test\s+automation|selenium|pytest|junit)\b',
    re.IGNORECASE
)


@dataclass
class RoleClassification:
    role_category: str
    confidence_score: float
    reason: str


def classify_role(title: str, description: str = "") -> RoleClassification:
    combined = f"{title} {description[:1000]}".lower()

    best_role = None
    best_count = 0
    reasons = []

    for role, patterns in ROLE_PATTERNS:
        count = sum(1 for p in patterns if re.search(p, combined, re.IGNORECASE))
        if count > best_count:
            best_count = count
            best_role = role
            reasons = [p for p in patterns if re.search(p, combined, re.IGNORECASE)]

    if not best_role:
        # Try keyword relevance
        kw_matches = RELEVANT_KEYWORDS.findall(combined)
        if len(kw_matches) >= 3:
            return RoleClassification(
                role_category="Software Engineer",
                confidence_score=0.40,
                reason=f"Keyword matches: {', '.join(set(kw_matches[:5]))}"
            )
        # Use "Other" (not "Not Relevant") so the frontend and filter logic
        # can treat it as a catch-all bucket rather than a sentinel value.
        return RoleClassification(
            role_category="Other",
            confidence_score=0.85,
            reason="No matching role patterns or relevant keywords found"
        )

    # Confidence from pattern count
    confidence = min(0.95, 0.50 + (best_count * 0.15))
    is_relevant = best_role in RELEVANT_ROLES

    return RoleClassification(
        role_category=best_role if is_relevant else "Other",
        confidence_score=confidence,
        reason=f"Matched {best_count} patterns: {', '.join(reasons[:3])}"
    )
