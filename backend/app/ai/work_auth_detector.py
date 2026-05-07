"""Work authorization detector — flags jobs that restrict candidates."""
import re
from dataclasses import dataclass, field

@dataclass
class WorkAuthResult:
    work_auth_flags: list[str] = field(default_factory=list)
    eligibility_risk_score: float = 0.0
    explanation: str = ""
    disqualified: bool = False

AUTH_PATTERNS: list[tuple[str, float, str]] = [
    # (pattern, risk_weight, flag_name)
    (r'\bno\s+sponsorship\b|\bwe\s+do\s+not\s+sponsor\b|\bsponsorship\s+not\s+available\b',
     0.8, "no_sponsorship"),
    (r'\bus\s+citizen(s?)\s+only\b|\bonly\s+us\s+citizens?\b|\bmust\s+be\s+a\s+us\s+citizen\b',
     0.9, "us_citizen_only"),
    (r'\bgreencard\s+only\b|\bgreen\s+card\s+only\b|\bgc\s+only\b|\bpermanent\s+resident\s+only\b',
     0.7, "greencard_only"),
    (r'\bsecurity\s+clearance\b|\bsecret\s+clearance\b|\btop\s+secret\b|\bts/sci\b',
     0.95, "security_clearance_required"),
    (r'\bno\s+c2c\b|\bcorp[\s-]to[\s-]corp\s+not\b',
     0.3, "no_c2c"),
    (r'\bw2\s+only\b|\bw-2\s+only\b',
     0.2, "w2_only"),
    (r'\b1099\b|\bindependent\s+contractor\b',
     0.1, "1099_contract"),
    (r'\blocal\s+only\b|\bno\s+relocation\b|\bno\s+remote\b',
     0.15, "local_only"),
    (r'\bauthorized\s+to\s+work\s+in\s+the\s+us\b|\bmust\s+be\s+authorized\b',
     0.2, "must_be_authorized"),
    (r'\beli[g]ible\s+to\s+work\b|\blegal\s+right\s+to\s+work\b',
     0.1, "work_eligibility_check"),
]

RISK_WEIGHTS = {
    "security_clearance_required": 0.95,
    "us_citizen_only": 0.90,
    "no_sponsorship": 0.80,
    "greencard_only": 0.70,
    "no_c2c": 0.30,
    "w2_only": 0.20,
    "1099_contract": 0.10,
    "local_only": 0.15,
    "must_be_authorized": 0.20,
    "work_eligibility_check": 0.10,
}

HIGH_RISK_FLAGS = {"security_clearance_required", "us_citizen_only", "greencard_only"}


def detect_work_auth(description: str, title: str = "") -> WorkAuthResult:
    combined = f"{title} {description}".lower()
    flags = []
    max_risk = 0.0

    for pattern, risk, flag_name in AUTH_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            flags.append(flag_name)
            max_risk = max(max_risk, RISK_WEIGHTS.get(flag_name, risk))

    if not flags:
        return WorkAuthResult(eligibility_risk_score=0.0, explanation="No work authorization restrictions detected")

    # Determine if disqualified by high-risk flags
    disqualified = any(f in HIGH_RISK_FLAGS for f in flags)

    explanations = []
    if "no_sponsorship" in flags:
        explanations.append("No visa sponsorship available")
    if "us_citizen_only" in flags:
        explanations.append("US citizens only")
    if "greencard_only" in flags:
        explanations.append("Green card / permanent resident only")
    if "security_clearance_required" in flags:
        explanations.append("Security clearance required")

    return WorkAuthResult(
        work_auth_flags=flags,
        eligibility_risk_score=round(max_risk, 2),
        explanation="; ".join(explanations) if explanations else f"Flags: {', '.join(flags)}",
        disqualified=disqualified,
    )
