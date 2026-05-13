"""
Advanced Job Filter & Scoring Engine.

Features:
  - Multi-factor weighted scoring (title, skills, experience, location, perks)
  - Configurable score weights via environment variables
  - Experience level detection from job descriptions
  - Salary range extraction
  - Location preference matching
  - Fuzzy title matching
  - Smart deduplication (title + company similarity)
"""

import re
import logging
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import urlparse, urlunparse

from config import (
    TARGET_TITLES,
    EXCLUDED_KEYWORDS,
    PREFERRED_LOCATIONS,
    MIN_EXPERIENCE_YEARS,
    MAX_EXPERIENCE_YEARS,
    SCORE_WEIGHT_TITLE,
    SCORE_WEIGHT_SKILLS,
    SCORE_WEIGHT_EXPERIENCE,
    SCORE_WEIGHT_LOCATION,
    SCORE_WEIGHT_PERKS,
)

logger = logging.getLogger(__name__)

# Precompile patterns for speed
_title_patterns = [re.compile(re.escape(t), re.IGNORECASE) for t in TARGET_TITLES]
_exclude_patterns = [re.compile(r"\b" + re.escape(k) + r"\b", re.IGNORECASE) for k in EXCLUDED_KEYWORDS]

# Explicit part-time / fractional signals (full-time is assumed for ATS boards unless matched)
_PART_TIME_SIGNALS = re.compile(
    r"\b(part[-\s]?time|per[-\s]?diem|(?:^|\s)\d{1,2}\s*hrs?/(?:week|wk)|"
    r"hours?\s+per\s+week|fractional\s+fte|0\.\d+\s*fte)\b",
    re.IGNORECASE,
)

# ─── Skill Categories (weighted by relevance) ──────────────────

SKILL_TIERS = {
    "tier1_core": {
        "weight": 3.0,
        "skills": [
            "python", "sql", "java", "go", "rust", "scala", "typescript",
        ],
    },
    "tier2_data": {
        "weight": 2.5,
        "skills": [
            "spark", "airflow", "kafka", "flink", "dbt", "dagster",
            "snowflake", "redshift", "bigquery", "databricks", "delta lake",
            "data pipeline", "data warehouse", "data lake", "etl", "elt",
            "data modeling", "data quality", "data governance",
        ],
    },
    "tier3_cloud": {
        "weight": 2.0,
        "skills": [
            "aws", "gcp", "azure", "docker", "kubernetes", "terraform",
            "ci/cd", "github actions", "jenkins", "argocd",
        ],
    },
    "tier4_ml": {
        "weight": 2.0,
        "skills": [
            "machine learning", "deep learning", "pytorch", "tensorflow",
            "mlflow", "mlops", "llm", "langchain", "vector database",
            "hugging face", "computer vision", "nlp", "rag",
        ],
    },
    "tier5_infra": {
        "weight": 1.5,
        "skills": [
            "postgresql", "mongodb", "redis", "elasticsearch", "cassandra",
            "dynamodb", "mysql", "clickhouse", "druid",
            "microservices", "distributed systems", "api", "rest", "graphql", "grpc",
            "linux", "git", "monitoring", "observability", "prometheus", "grafana",
        ],
    },
    "tier6_frontend": {
        "weight": 1.0,
        "skills": [
            "react", "node", "next.js", "vue", "angular", "tailwind",
            "html", "css", "javascript", "webpack",
        ],
    },
}

# Flatten for quick lookup
ALL_SKILLS = {}
for tier_name, tier_data in SKILL_TIERS.items():
    for skill in tier_data["skills"]:
        ALL_SKILLS[skill.lower()] = tier_data["weight"]

# Experience detection patterns
_exp_patterns = [
    re.compile(r"(\d+)\+?\s*(?:years?|yrs?)\s*(?:of\s*)?(?:experience|exp)", re.IGNORECASE),
    re.compile(r"(?:at least|minimum|min)\s*(\d+)\s*(?:years?|yrs?)", re.IGNORECASE),
    re.compile(r"(\d+)\s*-\s*(\d+)\s*(?:years?|yrs?)", re.IGNORECASE),
]

# Salary detection patterns
_salary_patterns = [
    re.compile(r"\$\s*([\d,]+)\s*(?:k|K)?\s*[-–to]+\s*\$?\s*([\d,]+)\s*(?:k|K)?", re.IGNORECASE),
    re.compile(r"\$\s*([\d,]+)\s*(?:k|K)", re.IGNORECASE),
    re.compile(r"([\d,]+)\s*(?:k|K)\s*[-–to]+\s*([\d,]+)\s*(?:k|K)", re.IGNORECASE),
]


# ─── Matching Functions ─────────────────────────────────────────

def is_title_match(title: str) -> bool:
    """Check if job title matches any target pattern, including fuzzy matching."""
    for pattern in _title_patterns:
        if pattern.search(title):
            return True

    # Fuzzy match for close titles
    title_lower = title.lower()
    for target in TARGET_TITLES:
        ratio = SequenceMatcher(None, title_lower, target).ratio()
        if ratio >= 0.75:
            return True

    return False


def normalize_job_url(url: str) -> str:
    """Normalize apply URL for deduplication."""
    if not url or not isinstance(url, str):
        return ""
    u = url.strip()
    try:
        p = urlparse(u.lower())
        # Strip common tracking params
        path = (p.path or "").rstrip("/")
        netloc = p.netloc
        return urlunparse((p.scheme, netloc, path, "", "", ""))
    except Exception:
        return u.lower().split("?", 1)[0].rstrip("/")


def is_full_time_role(title: str, description: str = "") -> bool:
    """Keep roles that look like standard full-time IC positions."""
    text = f"{title} {description[:2000]}".lower()
    if _PART_TIME_SIGNALS.search(text):
        return False
    return True


def is_excluded(title: str, description: str = "") -> bool:
    """Check if job should be excluded based on keywords."""
    text = f"{title} {description[:500]}".lower()
    for pattern in _exclude_patterns:
        if pattern.search(text):
            return True
    return False


def extract_experience_range(text: str) -> tuple[Optional[int], Optional[int]]:
    """Extract years of experience requirement from text."""
    text_lower = text.lower()

    for pattern in _exp_patterns:
        match = pattern.search(text_lower)
        if match:
            groups = match.groups()
            if len(groups) == 2 and groups[1]:
                return int(groups[0]), int(groups[1])
            elif len(groups) >= 1:
                years = int(groups[0])
                return years, years + 3  # Assume a range
    return None, None


def extract_salary_range(text: str) -> tuple[Optional[float], Optional[float]]:
    """Extract salary range from text."""
    for pattern in _salary_patterns:
        match = pattern.search(text)
        if match:
            groups = match.groups()
            try:
                val1 = float(groups[0].replace(",", ""))
                # Normalize K values
                if val1 < 1000:
                    val1 *= 1000
                if len(groups) >= 2 and groups[1]:
                    val2 = float(groups[1].replace(",", ""))
                    if val2 < 1000:
                        val2 *= 1000
                    return val1, val2
                return val1, None
            except (ValueError, IndexError):
                continue
    return None, None


# ─── Scoring Engine ─────────────────────────────────────────────

def compute_match_score(title: str, description: str = "", location: str = "") -> dict:
    """
    Compute a weighted match score (0-100) with detailed breakdown.

    Returns dict with 'total', 'title', 'skills', 'experience', 'location', 'perks'
    """
    text = f"{title} {description}".lower()
    scores = {"title": 0.0, "skills": 0.0, "experience": 0.0, "location": 0.0, "perks": 0.0}

    # ── Title Score (0 to SCORE_WEIGHT_TITLE) ──
    title_lower = title.lower()
    best_title_score = 0.0
    for target in TARGET_TITLES:
        if target in title_lower:
            ratio = len(target) / max(len(title_lower), 1)
            score = 0.5 + 0.5 * min(ratio * 2, 1)
            best_title_score = max(best_title_score, score)
        else:
            sim = SequenceMatcher(None, title_lower, target).ratio()
            if sim >= 0.7:
                best_title_score = max(best_title_score, sim * 0.6)

    scores["title"] = round(best_title_score * SCORE_WEIGHT_TITLE, 1)

    # ── Skills Score (0 to SCORE_WEIGHT_SKILLS) ──
    weighted_skill_hits = 0.0
    max_possible = 0.0
    matched_skills = []

    for skill, weight in ALL_SKILLS.items():
        max_possible += weight
        if skill in text:
            weighted_skill_hits += weight
            matched_skills.append(skill)

    if max_possible > 0:
        skill_ratio = min(1.0, (weighted_skill_hits / max_possible) * 5)  # Scale up since most jobs won't mention all
        scores["skills"] = round(skill_ratio * SCORE_WEIGHT_SKILLS, 1)

    # ── Experience Score (0 to SCORE_WEIGHT_EXPERIENCE) ──
    exp_min, exp_max = extract_experience_range(text)
    if exp_min is not None:
        if MIN_EXPERIENCE_YEARS <= exp_min <= MAX_EXPERIENCE_YEARS:
            scores["experience"] = SCORE_WEIGHT_EXPERIENCE  # Perfect fit
        elif exp_min <= MAX_EXPERIENCE_YEARS + 2:
            scores["experience"] = round(SCORE_WEIGHT_EXPERIENCE * 0.6, 1)  # Close enough
    else:
        scores["experience"] = round(SCORE_WEIGHT_EXPERIENCE * 0.5, 1)  # Unknown = neutral

    # ── Location Score (0 to SCORE_WEIGHT_LOCATION) ──
    loc_text = f"{location} {description[:2000]}".lower()
    if any(pref in loc_text for pref in PREFERRED_LOCATIONS):
        scores["location"] = SCORE_WEIGHT_LOCATION
    elif "remote" in loc_text or "hybrid" in loc_text or "anywhere" in loc_text:
        scores["location"] = round(SCORE_WEIGHT_LOCATION * 0.8, 1)
    elif location:
        scores["location"] = round(SCORE_WEIGHT_LOCATION * 0.3, 1)

    # ── Perks Score (0 to SCORE_WEIGHT_PERKS) ──
    perk_signals = 0
    perk_keywords = {
        "compensation": ["competitive salary", "equity", "stock options", "rsu", "401k", "bonus"],
        "flexibility": ["remote", "hybrid", "flexible", "work from home", "wfh"],
        "growth": ["growth", "career development", "mentorship", "learning budget", "conference"],
        "benefits": ["health insurance", "dental", "vision", "unlimited pto", "parental leave"],
        "seniority": ["senior", "staff", "principal", "lead", "architect", "director"],
    }
    for category, keywords in perk_keywords.items():
        if any(kw in text for kw in keywords):
            perk_signals += 1

    scores["perks"] = round((perk_signals / len(perk_keywords)) * SCORE_WEIGHT_PERKS, 1)

    # ── Total ──
    total = round(min(100, sum(scores.values())), 1)
    scores["total"] = total
    scores["matched_skills"] = matched_skills
    scores["experience_range"] = (exp_min, exp_max)

    return scores


# ─── Deduplication ──────────────────────────────────────────────

def deduplicate_jobs(jobs: list[dict]) -> list[dict]:
    """
    Remove duplicates using:
    1. Exact job_id match
    2. Same normalized apply URL
    3. Fuzzy title+company similarity (catches cross-platform dupes)
    """
    seen_ids = set()
    seen_links = set()
    seen_signatures = []
    unique = []

    for job in jobs:
        # Exact ID dedup
        if job["job_id"] in seen_ids:
            continue
        seen_ids.add(job["job_id"])

        link_key = normalize_job_url(job.get("link", ""))
        if link_key:
            if link_key in seen_links:
                continue
            seen_links.add(link_key)

        # Fuzzy dedup: same company + very similar title
        sig = (job.get("company", "").lower().strip(), job.get("title", "").lower().strip())
        is_dupe = False
        for existing_sig in seen_signatures:
            if sig[0] == existing_sig[0]:  # Same company
                title_sim = SequenceMatcher(None, sig[1], existing_sig[1]).ratio()
                if title_sim >= 0.90:  # Very similar title
                    is_dupe = True
                    break

        if not is_dupe:
            seen_signatures.append(sig)
            unique.append(job)

    if len(jobs) != len(unique):
        logger.info(f"Dedup removed {len(jobs) - len(unique)} duplicate jobs")

    return unique


# ─── Main Filter Pipeline ──────────────────────────────────────

def filter_jobs(jobs: list[dict]) -> list[dict]:
    """
    Full filtering pipeline:
    1. Title match (with fuzzy matching)
    2. Exclude non-relevant (intern, part-time, etc.)
    3. Deduplicate (exact + fuzzy)
    4. Score with multi-factor weighted scoring
    5. Extract metadata (experience, salary)
    6. Sort by score descending
    """
    logger.info(f"Filtering {len(jobs)} raw jobs...")

    # Step 1 & 2: Title + exclusion filter
    filtered = []
    for job in jobs:
        title = job.get("title", "")
        desc = job.get("description", "")

        if not is_title_match(title):
            continue
        if is_excluded(title, desc):
            continue
        if not is_full_time_role(title, desc):
            continue

        filtered.append(job)

    logger.info(f"After title/exclusion filter: {len(filtered)} jobs")

    # Step 3: Deduplicate
    filtered = deduplicate_jobs(filtered)
    logger.info(f"After dedup: {len(filtered)} jobs")

    # Step 4 & 5: Score + extract metadata
    for job in filtered:
        score_result = compute_match_score(
            job.get("title", ""),
            job.get("description", ""),
            job.get("location", ""),
        )
        job["match_score"] = score_result["total"]
        job["score_breakdown"] = {
            k: v for k, v in score_result.items()
            if k not in ("total", "matched_skills", "experience_range")
        }
        job["matched_skills"] = score_result.get("matched_skills", [])

        # Extract experience range
        exp_min, exp_max = score_result.get("experience_range", (None, None))
        job["experience_min"] = exp_min
        job["experience_max"] = exp_max

        # Extract salary range
        salary_min, salary_max = extract_salary_range(job.get("description", ""))
        job["salary_min"] = salary_min
        job["salary_max"] = salary_max

    # Step 6: Sort by score
    filtered.sort(key=lambda x: x["match_score"], reverse=True)

    top = filtered[0]["match_score"] if filtered else 0
    avg = round(sum(j["match_score"] for j in filtered) / len(filtered), 1) if filtered else 0

    logger.info(
        f"Filter complete: {len(filtered)} relevant jobs | "
        f"Top: {top} | Avg: {avg}"
    )
    return filtered


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sample = [
        {
            "job_id": "a1", "source": "test", "company": "Stripe",
            "title": "Senior Data Engineer",
            "description": "Python, Spark, Airflow, AWS, SQL, Kafka, distributed systems. "
                           "5+ years experience. Remote. Competitive salary with equity.",
            "location": "Remote", "link": "#",
        },
        {
            "job_id": "a2", "source": "test", "company": "TestCo",
            "title": "Marketing Intern",
            "description": "Social media", "location": "NYC", "link": "#",
        },
        {
            "job_id": "a3", "source": "test", "company": "Databricks",
            "title": "Software Engineer — Platform",
            "description": "Java, Kubernetes, microservices, distributed systems, Docker, CI/CD, "
                           "AWS, Terraform. 3-5 years. $180K-$250K.",
            "location": "San Francisco, CA", "link": "#",
        },
    ]
    result = filter_jobs(sample)
    for j in result:
        print(f"  [{j['match_score']:5.1f}] {j['company']}: {j['title']}")
        print(f"         Skills: {', '.join(j.get('matched_skills', [])[:8])}")
        if j.get("salary_min"):
            print(f"         Salary: ${j['salary_min']:,.0f} - ${j.get('salary_max', 0):,.0f}")
