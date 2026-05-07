"""
Normalizer — location parsing, country detection, remote classification,
skill normalization, role classification, experience level.

Design principles:
  - Pure functions, no I/O, no external deps
  - Always return a value (never None for strings — use "unknown" as sentinel)
  - ISO 3166-1 alpha-2 for country codes
  - Expand inputs: check title + description for remote/role signals
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Country keyword → ISO code map ────────────────────────────────────────────
# Each entry: (list_of_lowercase_keywords, ISO_code)
# Ordered from most-specific to least-specific to prevent false matches.
_COUNTRY_MAP: list[tuple[list[str], str]] = [
    # ---- North America ----
    (["united states", "usa", "u.s.a", "u.s.",
      # positional patterns — cover "Remote - US", "US - Remote", "US only", etc.
      " us,", ", us ", "(us)", "- us", "us -", " us ", "us only",
      # major US cities
      "new york", "new york city", "nyc", "san francisco", "seattle", "austin",
      "boston", "chicago", "los angeles", "denver", "atlanta", "dallas",
      "washington dc", "washington d.c"], "US"),
    (["canada", "toronto", "vancouver", "montreal", "calgary", "ottawa",
      "edmonton", "winnipeg", ", on,", ", bc,", ", ab,", ", qc,"], "CA"),
    (["mexico", "ciudad de mexico", "guadalajara", "monterrey", "cdmx"], "MX"),
    (["brazil", "são paulo", "sao paulo", "rio de janeiro", "brasilia", "curitiba"], "BR"),
    # ---- Europe ----
    (["united kingdom", "england", "scotland", "wales", "northern ireland",
      "london", "manchester", "birmingham", "edinburgh", "bristol", "leeds",
      "glasgow", "liverpool", ", uk,", "(uk)", "- uk"], "GB"),
    (["germany", "berlin", "munich", "münchen", "hamburg", "frankfurt",
      "cologne", "köln", "düsseldorf", "stuttgart", "deutschland"], "DE"),
    (["netherlands", "amsterdam", "rotterdam", "the hague", "eindhoven",
      "utrecht", "hague"], "NL"),
    (["ireland", "dublin", "cork", "galway"], "IE"),
    (["france", "paris", "lyon", "toulouse", "marseille", "nantes"], "FR"),
    (["spain", "madrid", "barcelona", "seville", "valencia", "bilbao"], "ES"),
    (["sweden", "stockholm", "gothenburg", "malmö", "göteborg"], "SE"),
    (["switzerland", "zurich", "zürich", "geneva", "bern", "lausanne"], "CH"),
    (["poland", "warsaw", "krakow", "wroclaw", "poznan", "gdansk"], "PL"),
    (["portugal", "lisbon", "porto"], "PT"),
    (["denmark", "copenhagen"], "DK"),
    (["norway", "oslo", "bergen"], "NO"),
    (["finland", "helsinki"], "FI"),
    (["austria", "vienna", "wien"], "AT"),
    (["belgium", "brussels", "antwerp", "ghent"], "BE"),
    (["italy", "rome", "milan", "milano", "florence", "turin", "torino"], "IT"),
    (["czech republic", "czechia", "prague", "brno"], "CZ"),
    (["romania", "bucharest", "cluj"], "RO"),
    (["ukraine", "kyiv", "lviv", "kharkiv"], "UA"),
    # ---- Asia-Pacific ----
    (["india", "bangalore", "bengaluru", "mumbai", "delhi", "new delhi",
      "hyderabad", "chennai", "pune", "kolkata", "noida", "gurugram",
      "gurgaon", "ahmedabad", "jaipur"], "IN"),
    (["singapore"], "SG"),
    (["australia", "sydney", "melbourne", "brisbane", "perth", "adelaide",
      "canberra"], "AU"),
    (["new zealand", "auckland", "wellington", "christchurch"], "NZ"),
    (["japan", "tokyo", "osaka", "kyoto", "yokohama", "nagoya"], "JP"),
    (["china", "beijing", "shanghai", "shenzhen", "guangzhou", "chengdu"], "CN"),
    (["hong kong", " hk,", "(hk)"], "HK"),
    (["south korea", "korea", "seoul", "busan"], "KR"),
    (["taiwan", "taipei"], "TW"),
    # ---- Middle East / Africa ----
    (["united arab emirates", "uae", "dubai", "abu dhabi"], "AE"),
    (["israel", "tel aviv", "jerusalem", "haifa"], "IL"),
    (["south africa", "johannesburg", "cape town", "durban"], "ZA"),
    (["egypt", "cairo", "alexandria"], "EG"),
    (["nigeria", "lagos", "abuja"], "NG"),
    (["kenya", "nairobi"], "KE"),
]

# Two-letter codes that can appear as the last segment of a location string
_ISO2_SET: frozenset[str] = frozenset({
    "US", "GB", "UK", "IN", "CA", "AU", "DE", "NL", "SG", "IE",
    "FR", "ES", "PL", "PT", "SE", "CH", "BR", "MX", "NZ", "IL",
    "AE", "JP", "CN", "HK", "KR", "DK", "NO", "FI", "AT", "BE",
    "IT", "CZ", "RO", "UA", "TW", "ZA", "EG", "NG", "KE", "TW",
})

# Issue 7: US state/territory abbreviations.
# These MUST resolve to "US", not to "CA" (Canada) or any other ISO-2 code.
# Checked BEFORE the generic ISO-2 last-resort so that "San Jose, CA" and
# "Phoenix, AZ" correctly return "US" instead of "CA" / "unknown".
_US_STATE_ABBREVS: frozenset[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
})

# ── Skill alias normalization ──────────────────────────────────────────────────
# Maps common aliases / abbreviations to canonical skill names.
SKILL_NORMALIZATION_MAP: dict[str, str] = {
    "py": "Python",
    "python3": "Python",
    "python 3": "Python",
    "nodejs": "Node.js",
    "node js": "Node.js",
    "node": "Node.js",
    "reactjs": "React",
    "react js": "React",
    "vuejs": "Vue.js",
    "vue js": "Vue.js",
    "angular js": "Angular",
    "angularjs": "Angular",
    "aws cloud": "AWS",
    "amazon aws": "AWS",
    "amazon web services": "AWS",
    "google cloud platform": "GCP",
    "google cloud": "GCP",
    "k8s": "Kubernetes",
    "kube": "Kubernetes",
    "tf": "Terraform",
    "postgres": "PostgreSQL",
    "pg": "PostgreSQL",
    "mongo": "MongoDB",
    "elastic": "Elasticsearch",
    "opensearch": "Elasticsearch",
    "cicd": "CI/CD",
    "ci cd": "CI/CD",
    "c plus plus": "C++",
    "cpp": "C++",
    "golang": "Go",
    "go lang": "Go",
    "ts": "TypeScript",
    "js": "JavaScript",
    "sklearn": "Scikit-learn",
    "scikit learn": "Scikit-learn",
    "huggingface": "Hugging Face",
    "data build tool": "dbt",
    "adf": "Azure Data Factory",
    "big query": "BigQuery",
    "apache spark": "Spark",
    "apache kafka": "Kafka",
    "apache airflow": "Airflow",
    "apache flink": "Flink",
    "trino": "Presto",
    "large language model": "LLMs",
    "large language models": "LLMs",
    "llm": "LLMs",
}

# Core skills used by decision engine for weighted scoring
CORE_SKILLS: frozenset[str] = frozenset({
    "Python", "SQL", "Java", "Scala", "Go", "C++", "TypeScript", "JavaScript",
    "AWS", "GCP", "Azure", "Docker", "Kubernetes",
    "Spark", "PySpark", "Kafka", "Airflow", "dbt",
    "TensorFlow", "PyTorch", "Scikit-learn",
    "PostgreSQL", "MySQL", "MongoDB", "Redis", "Elasticsearch",
    "React", "Node.js", "FastAPI", "Django", "Spring Boot",
    "LLMs", "Hugging Face", "LangChain",
})

# Secondary skills (nice-to-have, lower penalty weight)
SECONDARY_SKILLS: frozenset[str] = frozenset({
    "Jira", "Figma", "Confluence", "Notion",
    "Excel", "Tableau", "Power BI", "Looker",
    "Git", "GitHub", "GitLab",
    "Terraform", "CI/CD",
    "Postman", "Swagger",
})


def normalize_skill(raw: str) -> str:
    """Return canonical name for a skill alias, or the original if unknown."""
    return SKILL_NORMALIZATION_MAP.get(raw.lower().strip(), raw)


# ── Title normalizer ───────────────────────────────────────────────────────────

def normalize_title(title: str) -> str:
    if not title:
        return ""
    title = re.sub(r'[\(\[].*?[\)\]]', '', title)
    return ' '.join(title.split()).strip()


# ── Location normalizer ────────────────────────────────────────────────────────

def normalize_location(location: str) -> str:
    if not location:
        return ""
    parts = [p.strip() for p in location.split(',')]
    if len(parts) >= 2:
        return f"{parts[0]}, {parts[1]}"
    return location.strip()


def parse_location(location: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Return (city, region) from a freeform location string.

    Examples:
      "San Francisco, CA, US"  → ("San Francisco", "CA")
      "London, UK"             → ("London", None)
      "Remote"                 → (None, None)
    """
    if not location:
        return None, None

    # Strip parenthetical notes like "(Remote)" or "(Hybrid)"
    location = re.sub(r'\(.*?\)', '', location).strip()

    parts = [p.strip() for p in location.split(',')]
    parts = [p for p in parts if p]

    if not parts:
        return None, None

    city = parts[0] if parts[0].lower() not in ("remote", "worldwide", "global", "anywhere") else None

    # Second part is region only if it's not a country code / country name
    # Strings that look like a region segment but are actually country identifiers.
    # Includes both ISO-2 codes and common full country names.
    _not_region = {
        # ISO-2 codes
        "US", "USA", "UK", "GB", "IN", "AU", "DE", "NL", "SG", "IE",
        "FR", "ES", "SE", "CH", "PL", "PT", "DK", "NO", "FI", "AT",
        "BE", "IT", "CZ", "RO", "UA", "TW", "ZA", "EG", "NG", "KE",
        "CA", "MX", "BR", "NZ", "IL", "AE", "JP", "CN", "HK", "KR",
        # Full country names
        "United States", "United Kingdom", "India", "Canada", "Australia",
        "Germany", "Netherlands", "Ireland", "France", "Spain", "Sweden",
        "Switzerland", "Poland", "Portugal", "Denmark", "Norway", "Finland",
        "Austria", "Belgium", "Italy", "Singapore", "Japan", "China",
        "Brazil", "Mexico", "Israel", "New Zealand", "South Africa",
        "South Korea", "Hong Kong", "Taiwan", "Ukraine",
        # Remote/global markers
        "Remote", "Worldwide", "Global", "Anywhere",
    }
    # If there are 3+ segments and the last is a known country identifier,
    # treat the second segment as region unconditionally (handles "City, State, Country").
    last_is_country = len(parts) >= 3 and parts[-1] in _not_region
    if last_is_country and len(parts) > 1:
        region = parts[1] if parts[1] else None
    else:
        region = (
            parts[1]
            if len(parts) > 1 and parts[1] not in _not_region
            else None
        )

    return city or None, region or None


def normalize_country(location: str) -> str:
    """
    Return ISO 3166-1 alpha-2 country code from a freeform location string.
    Returns "unknown" (never None) when no match found.
    """
    if not location:
        return "unknown"

    # Pad with spaces to enable whole-word boundary matching on the keywords
    loc_lower = f" {location.lower()} "

    for keywords, iso_code in _COUNTRY_MAP:
        for kw in keywords:
            if kw in loc_lower:
                # Normalise legacy "UK" to ISO "GB"
                return "GB" if iso_code == "UK" else iso_code

    # Issue 7: US state abbreviation check — must run BEFORE the ISO-2 fallback.
    # "San Jose, CA" and "Austin, TX" contain a US state code, NOT a country code.
    # If any CSV segment is a known US state abbreviation, the location is US.
    # Canadian cities (Toronto, Vancouver, Ottawa, Calgary) are caught by keyword
    # matching above and never reach this block, so no false positives.
    segments = [s.strip().upper() for s in location.split(",")]
    for seg in segments:
        if seg in _US_STATE_ABBREVS:
            return "US"

    # Last-resort: check if the last CSV segment is a bare ISO-2 code
    if segments:
        candidate = segments[-1]
        if candidate in _ISO2_SET:
            return "GB" if candidate == "UK" else candidate

    return "unknown"


def normalize_remote(location: str, title: str = "", text: str = "") -> str:
    """
    Classify work modality from location, title, and job description.

    Checks hybrid before remote — "hybrid" is often mentioned alongside "remote".
    Returns: "remote" | "hybrid" | "onsite" | "unknown"
    """
    combined = f"{location} {title} {text}".lower()

    if re.search(r'\bhybrid\b', combined):
        return "hybrid"
    if re.search(
        r'\bremote\b|\banywhere\b|\bwork[\s-]from[\s-]home\b|\bwfh\b'
        r'|fully[\s-]remote|100%\s+remote|distributed\s+team',
        combined
    ):
        return "remote"
    if re.search(r'\bonsite\b|\bon[\s-]site\b|\bin[\s-]office\b|\bin[\s-]person\b', combined):
        return "onsite"
    # If a concrete city/region is in the location with no remote signal → onsite
    if location and location.strip().lower() not in ("", "remote", "worldwide", "global", "anywhere"):
        return "onsite"
    return "unknown"


def normalize_currency(text: str) -> Optional[str]:
    """Detect salary currency from description text."""
    if not text:
        return None
    if "£" in text or "gbp" in text.lower():
        return "GBP"
    if "€" in text or "eur" in text.lower():
        return "EUR"
    if "₹" in text or "inr" in text.lower() or "lakh" in text.lower():
        return "INR"
    if "$" in text or "usd" in text.lower():
        return "USD"
    return None


# ── Experience level ───────────────────────────────────────────────────────────

def classify_experience_level(title: str, description: str = "") -> str:
    combined = f"{title} {description[:600]}".lower()
    if re.search(r'\bintern\b|\binternship\b|\bco[\s-]?op\b', combined):
        return "intern"
    if re.search(
        r'\bjunior\b|\bentry[\s-]?level\b|\bnew\s+grad\b|\brecent\s+grad'
        r'|\bgraduate\b|\bjr\.?\b|\b0[\s-]?[–-]\s*[12]\s+years?\b',
        combined
    ):
        return "entry"
    if re.search(
        r'\bstaff\b|\bprincipal\b|\bdirector\b|\bvp\b|\bvice\s+president'
        r'|\bhead\s+of\b|\bdistinguished\b|\bfellow\b',
        combined
    ):
        return "staff"
    if re.search(
        r'\bsenior\b|\blead\b|\bsr\.?\b|\bmanager\b'
        r'|\b[5-9]\+\s+years?\b|\b1[0-9]\+\s+years?\b',
        combined
    ):
        return "senior"
    return "mid"


# ── Role category ──────────────────────────────────────────────────────────────
# This is a fast first-pass classifier used by the pipeline.
# The full role_classifier.py does a deeper pattern-count pass with description.

_ROLE_RULES: list[tuple[str, str]] = [
    # AI / ML first (most specific)
    (r'\bai\s+\w*\s*engineer\b|\bllm\s+engineer\b|\bgenai\b|\bgenerative\s+ai\b'
     r'|\bprompt\s+engineer\b|\bai\s+platform\b|\bfoundation\s+model\b'
     r'|\bartificial\s+intelligence\s+engineer\b',
     "AI Engineer"),
    (r'\bml\s+engineer\b|\bmachine\s+learning\s+engineer\b'
     r'|\bdeep\s+learning\s+engineer\b|\bml\s+platform\b',
     "ML Engineer"),
    (r'\bmlops\b|\bml\s+ops\b|\bml\s+infrastructure\b|\bmodel\s+serving\b'
     r'|\bkubeflow\b|\bmlflow\b|\bvertex\s+ai\b',
     "MLOps Engineer"),
    # Data
    (r'\bdata\s+platform\s+engineer\b|\blakehouse\b|\bdelta\s+lake\b', "Data Platform Engineer"),
    (r'\banalytics\s+engineer\b|\bdbt\s+engineer\b|\bsql\s+engineer\b', "Analytics Engineer"),
    (r'\bdata\s+engineer\b|\bdata\s+pipeline\b|\betl\b|\belt\b'
     r'|\bpyspark\b|\bspark\s+engineer\b|\bdata\s+infrastructure\b',
     "Data Engineer"),
    (r'\bdata\s+scientist\b', "Data Scientist"),
    (r'\bdata\s+analyst\b|\banalytics\s+analyst\b', "Data Analyst"),
    # Engineering
    (r'\bsdet\b|\bqa\s+engineer\b|\btest\s+automation\b|\bquality\s+assurance\b'
     r'|\bautomation\s+engineer\b|\bperformance\s+test\b',
     "QA/SDET"),
    (r'\bdevops\b|\bsre\b|\bsite\s+reliability\b|\bplatform\s+engineer\b'
     r'|\bcloud\s+engineer\b|\binfrastructure\s+engineer\b',
     "DevOps"),
    (r'\bbackend\b|\bback[\s-]end\b|\bapi\s+engineer\b'
     r'|\bserver[\s-]side\b|\bmicroservices\b',
     "Backend Engineer"),
    (r'\bfrontend\s+engineer\b|\bfront[\s-]end\s+engineer\b|\bui\s+engineer\b'
     r'|\breact\s+developer\b|\bvue\s+developer\b',
     "Frontend Engineer"),
    (r'\bfull[\s-]?stack\b', "Fullstack Engineer"),
    (r'\bsoftware\s+engineer\b|\bsoftware\s+developer\b|\bswe\b', "Software Engineer"),
    # Non-technical (filter noise)
    (r'\bproduct\s+manager\b|\bpm\b|\bproduct\s+owner\b', "Product"),
    (r'\bsales\b|\baccount\s+executive\b|\bsdr\b|\bbdr\b|\bsales\s+engineer\b|\bclient\s+success\b|\baccount\s+manager\b', "Sales"),
    (r'\brecruiter\b|\btalent\s+acquisition\b|\bhr\b|\bhuman\s+resources\b|\bpeople\s+ops\b', "HR/Recruiting"),
    (r'\bmarketing\b|\bgrowth\b|\bcontent\b|\bseo\b|\bdesign\b|\bux\b|\bui/ux\b|\bbrand\b', "Marketing/Design"),
    (r'\bfinance\b|\baccounting\b|\bcfo\b|\bcontroller\b|\bwealth\b|\btax\b', "Finance"),
    (r'\boperations\b|\bops\b|\bstrategy\b|\bplanning\b|\bchief\s+of\s+staff\b|\business\s+ops\b', "Operations/Strategy"),
    (r'\bsupport\b|\bcustomer\s+support\b|\bhelp\s+desk\b|\bservice\s+engineer\b|\btechnical\s+support\b', "Support/Service"),
]


def classify_role_category(title: str, description: str = "") -> str:
    """
    Return role category string from job title and optional description snippet.

    Uses ordered regex rules — first match wins (most-specific rules are first).
    Falls back to "Other" when no rule matches.
    """
    combined = f"{title} {description[:800]}".lower()
    for pattern, category in _ROLE_RULES:
        if re.search(pattern, combined):
            return category
    return "Other"


# ── Employment type detection ──────────────────────────────────────────────────

_EMPLOYMENT_TYPE_RULES: list[tuple[str, str]] = [
    (r'\bcontract\b|\bcontractor\b|\bfreelance\b|\bc2c\b|\bcorp[\s-]to[\s-]corp\b', "contract"),
    (r'\binternship\b|\bintern\b|\bco[\s-]?op\b', "internship"),
    (r'\bpart[\s-]time\b|\bpart\s+time\b', "part-time"),
    (r'\bfull[\s-]time\b|\bfull\s+time\b|\bpermanent\b', "full-time"),
    (r'\btemporary\b|\btemp\b', "temporary"),
]


def _detect_employment_type(text: str) -> str:
    """Detect employment type from job description or title text."""
    if not text:
        return "full-time"
    text_lower = text.lower()
    for pattern, emp_type in _EMPLOYMENT_TYPE_RULES:
        if re.search(pattern, text_lower):
            return emp_type
    return "full-time"


def _detect_salary_period(text: str) -> Optional[str]:
    """Detect salary period (annual/monthly/hourly) from description text."""
    if not text:
        return None
    text_lower = text.lower()
    if re.search(r'\bper\s+hour\b|\bhourly\b|\b/hr\b|\bper\s+hr\b', text_lower):
        return "hourly"
    if re.search(r'\bper\s+month\b|\bmonthly\b|\b/month\b|\bper\s+mo\b', text_lower):
        return "monthly"
    if re.search(
        r'\bper\s+year\b|\bannual\b|\bannually\b|\b/year\b|\ba\s+year\b|\bpa\b', text_lower
    ):
        return "annual"
    return None


# ── Step 2: Fingerprint ─────────────────────────────────────────────────────────
# Deliberately based ONLY on (title + company_name) so that the same role posted
# at multiple office locations is still treated as the same job.  Location is
# intentionally excluded: "Senior Python Engineer @ Acme (SF)" and the same job
# listed for "Acme (NYC)" should share a fingerprint and be deduplicated.

def compute_fingerprint(title: str, company_name: str, location: str = "") -> str:
    """
    Return a 64-char SHA-256 fingerprint for a job posting.

    Keyed on (normalized title, normalized company name, normalized city/region)
    so that genuinely different location-specific postings for the same role
    are treated as distinct jobs (e.g. "SWE" at Google NYC ≠ "SWE" at Google SF).

    Location is reduced to just the first segment (city name) before the first
    comma to avoid noise from address formatting differences while still
    differentiating cross-city postings.

    Args:
        title:        Job title (any casing; will be lowercased + stripped).
        company_name: Company name (any casing; will be lowercased + stripped).
        location:     Full location string — only the city part is used.

    Returns:
        64-character hex string.
    """
    import re
    # Normalize location: take only the first comma-segment (city),
    # strip digits, extra whitespace, and lowercase.
    city_part = location.split(",")[0].strip().lower() if location else ""
    city_part = re.sub(r"\s+", " ", re.sub(r"[^a-z\s]", "", city_part)).strip()

    key = f"{title.lower().strip()}::{company_name.lower().strip()}::{city_part}"
    return hashlib.sha256(key.encode()).hexdigest()[:64]


# ── Step 3: source_type normalization ─────────────────────────────────────────
# Canonical allowed values: "DIRECT_COMPANY" | "STAFFING_AGENCY" | "UNKNOWN"
# Maps all raw/partial/legacy values to one of these three canonical strings.

_SOURCE_TYPE_MAP: dict[str, str] = {
    # Direct company variants
    "direct":           "DIRECT_COMPANY",
    "direct_company":   "DIRECT_COMPANY",
    "direct company":   "DIRECT_COMPANY",
    "company":          "DIRECT_COMPANY",
    "ats":              "DIRECT_COMPANY",
    "direct_ats":       "DIRECT_COMPANY",
    # Staffing / agency variants
    "staffing":         "STAFFING_AGENCY",
    "staffing_agency":  "STAFFING_AGENCY",
    "staffing agency":  "STAFFING_AGENCY",
    "agency":           "STAFFING_AGENCY",
    "recruiting":       "STAFFING_AGENCY",
    "recruiter":        "STAFFING_AGENCY",
    "vendor":           "STAFFING_AGENCY",
    "consulting_vendor":"STAFFING_AGENCY",
    "consulting vendor":"STAFFING_AGENCY",
}


def normalize_source_type(raw: str) -> str:
    """
    Map any raw source_type string to a canonical value.

    Canonical values:
        "DIRECT_COMPANY"  — job posted directly by the hiring company
        "STAFFING_AGENCY" — posted by a recruiter / staffing firm
        "UNKNOWN"         — unable to classify

    Variants like "direct", "company", "ats" → "DIRECT_COMPANY"
    Variants like "agency", "staffing", "vendor" → "STAFFING_AGENCY"
    """
    if not raw:
        return "DIRECT_COMPANY"   # safe default: most pipeline jobs are direct ATS
    normalized = raw.strip().lower().replace("-", "_").replace(" ", "_")
    # Already canonical — return as-is
    if raw.upper() in ("DIRECT_COMPANY", "STAFFING_AGENCY", "UNKNOWN"):
        return raw.upper()
    return _SOURCE_TYPE_MAP.get(raw.strip().lower(), "UNKNOWN")


# ── Step 9: Data quality score ─────────────────────────────────────────────────

def compute_data_quality_score(
    description: str,
    required_skills: list,
    country: str,
    role_category: str,
) -> float:
    """
    Return a 0.0–1.0 quality score for an enriched job.

    Scoring:
        0.25  — has at least one required skill extracted
        0.25  — description is substantive (≥ 100 chars)
        0.25  — country resolved to something other than "unknown"
        0.25  — role classified to something other than "Other"

    Stored in Job.data_quality_score so downstream systems can filter/sort
    by data completeness without re-computing on every query.
    """
    score = 0.0
    if required_skills:
        score += 0.25
    if len(description or "") >= 100:
        score += 0.25
    if country and country not in ("unknown", "", None):
        score += 0.25
    if role_category and role_category not in ("Other", "", None):
        score += 0.25
    return round(score, 2)


# ── Step 1: Hardened normalize_job ─────────────────────────────────────────────

def _get_field(obj: Any, *keys: str, default: Any = "") -> Any:
    """
    Duck-type getter that works for both dicts and dataclass/ORM objects.

    Tries each key in order; returns the first non-None / non-empty value found.
    Falls back to `default` when nothing matches.
    """
    for key in keys:
        if isinstance(obj, dict):
            val = obj.get(key)
        else:
            val = getattr(obj, key, None)
        if val is not None and val != "":
            return val
    return default


def normalize_job(raw_job: Any) -> Optional[dict]:
    """
    Produce the comprehensive Silver-layer normalized dict from a raw job.

    Accepts EITHER:
      - A RawJob dataclass (app.connectors.base.RawJob)
      - A plain Python dict (e.g. from aggregator feeds or tests)

    Validation (Step 1):
      Returns None — with a structured warning log — if any of the three
      required fields are empty after stripping:
        • title        → missing_title
        • company_name → missing_company
        • job_url      → missing_job_url

    Safe defaults (Step 1):
      city, region   → None when parse_location returns None
      source_type    → "DIRECT_COMPANY" when not present in raw_job

    Fingerprint (Step 2):
      Computed from (title + company) ONLY — no location.
      Callers should NOT call compute_fingerprint() again after normalize_job().

    Returns
    -------
    dict with ALL of the following keys (or None on validation failure):

      Required silver fields:
        title, company, job_url, location, city, region, country,
        remote_type, source_type, fingerprint

      Classification fields:
        employment_type, experience_level, salary_period,
        normalized_title, normalized_location

      Pass-through fields:
        description, description_html, salary_min, salary_max, salary_currency
    """
    # ── Extract raw values (dict or object) ────────────────────────────────────
    raw_title   = _get_field(raw_job, "title")
    raw_company = _get_field(raw_job, "company_name", "company")
    raw_job_url = _get_field(raw_job, "job_url")
    raw_location = _get_field(raw_job, "location", default="")
    raw_description = _get_field(raw_job, "description", default="")
    raw_desc_html   = _get_field(raw_job, "description_html", default="")
    raw_emp_type    = _get_field(raw_job, "employment_type", default="")
    raw_src_type    = _get_field(raw_job, "source_type", default="")
    raw_salary_min  = _get_field(raw_job, "salary_min", default=None)
    raw_salary_max  = _get_field(raw_job, "salary_max", default=None)
    raw_salary_curr = _get_field(raw_job, "salary_currency", default="")

    # ── Validate required fields (Step 1) ─────────────────────────────────────
    title   = normalize_title(str(raw_title).strip())
    company = str(raw_company).strip()
    job_url = str(raw_job_url).strip()

    if not title:
        logger.warning(
            "normalize_job_skip reason=missing_title raw_company=%r raw_url=%r",
            raw_company, raw_job_url,
        )
        return None

    if not company:
        logger.warning(
            "normalize_job_skip reason=missing_company title=%r raw_url=%r",
            title, raw_job_url,
        )
        return None

    if not job_url:
        logger.warning(
            "normalize_job_skip reason=missing_job_url title=%r company=%r",
            title, company,
        )
        return None

    # ── Normalize location ─────────────────────────────────────────────────────
    location_raw = str(raw_location).strip()
    location     = normalize_location(location_raw)
    city, region = parse_location(location_raw)       # both can be None
    country      = normalize_country(location_raw)
    remote_type  = normalize_remote(location_raw, title, str(raw_description))

    # ── Classification ─────────────────────────────────────────────────────────
    description      = str(raw_description).strip()
    description_html = str(raw_desc_html).strip()
    experience_level = classify_experience_level(title, description)
    employment_type  = (
        str(raw_emp_type).strip()
        or _detect_employment_type(f"{title} {description[:500]}")
    )
    salary_period   = _detect_salary_period(description)
    salary_currency = (
        str(raw_salary_curr).strip()
        or normalize_currency(description)
        or "USD"
    )

    # ── source_type (Step 3) ───────────────────────────────────────────────────
    source_type = normalize_source_type(str(raw_src_type))

    # ── Fingerprint (Step 2) ───────────────────────────────────────────────────
    fingerprint = compute_fingerprint(title, company, location)

    return {
        # Required / validated fields
        "title":               title,
        "company":             company,
        "job_url":             job_url,
        "location":            location,
        "city":                city,           # None if not parseable
        "region":              region,         # None if not parseable
        "country":             country,
        "remote_type":         remote_type,
        "source_type":         source_type,
        "fingerprint":         fingerprint,
        # Classification
        "employment_type":     employment_type,
        "experience_level":    experience_level,
        "salary_period":       salary_period,
        "normalized_title":    title.lower(),
        "normalized_location": location.lower(),
        # Pass-through
        "description":         description,
        "description_html":    description_html,
        "salary_min":          raw_salary_min,
        "salary_max":          raw_salary_max,
        "salary_currency":     salary_currency,
    }
