"""Skill extraction engine — extracts required and preferred skills from descriptions."""
import re
from dataclasses import dataclass, field
from typing import Optional

# Core skills get priority ordering within each bucket (required / preferred).
# Defined here to avoid a circular import with normalizer.py.
_CORE_SKILLS: frozenset[str] = frozenset({
    "Python", "SQL", "Java", "Scala", "Go", "C++", "TypeScript", "JavaScript",
    "AWS", "GCP", "Azure", "Docker", "Kubernetes",
    "Spark", "PySpark", "Kafka", "Airflow", "dbt",
    "TensorFlow", "PyTorch", "Scikit-learn",
    "PostgreSQL", "MySQL", "MongoDB", "Redis", "Elasticsearch",
    "React", "Node.js", "FastAPI", "Django", "Spring Boot",
    "LLMs", "Hugging Face", "LangChain",
})

# Hard caps — keeps payloads lean and prevents skill-heavy JDs from drowning signal.
_MAX_REQUIRED = 15
_MAX_PREFERRED = 10

SKILL_CATALOG: dict[str, list[str]] = {
    # Programming
    "Python": [r'\bpython\b'],
    "Java": [r'\bjava\b(?!\s*script)'],
    "Scala": [r'\bscala\b'],
    "SQL": [r'\bsql\b', r'\bt-sql\b', r'\bplsql\b', r'\bpl/sql\b'],
    "JavaScript": [r'\bjavascript\b', r'\bjs\b'],
    "TypeScript": [r'\btypescript\b', r'\bts\b'],
    "Go": [r'\bgolang\b', r'\bgo\s+programming\b'],
    "Rust": [r'\brust\b(?!\s+belt)'],
    "C++": [r'\bc\+\+\b', r'\bcpp\b'],
    "R": [r'\br\s+programming\b', r'\brlang\b'],

    # Data Tools
    "Spark": [r'\bapache\s+spark\b', r'\bspark\b(?!\s+plug)'],
    "PySpark": [r'\bpyspark\b'],
    "Kafka": [r'\bapache\s+kafka\b', r'\bkafka\b'],
    "Airflow": [r'\bapache\s+airflow\b', r'\bairflow\b'],
    "dbt": [r'\bdbt\b', r'\bdata\s+build\s+tool\b'],
    "Snowflake": [r'\bsnowflake\b'],
    "Databricks": [r'\bdatabricks\b'],
    "BigQuery": [r'\bbigquery\b', r'\bbig\s+query\b'],
    "Redshift": [r'\bredshift\b'],
    "AWS Glue": [r'\baws\s+glue\b', r'\bglue\s+etl\b'],
    "Azure Data Factory": [r'\bazure\s+data\s+factory\b', r'\badf\b'],
    "Delta Lake": [r'\bdelta\s+lake\b', r'\bdelta\s+tables?\b'],
    "Flink": [r'\bapache\s+flink\b', r'\bflink\b'],
    "Hive": [r'\bhive\b', r'\bapache\s+hive\b'],
    "Presto": [r'\bpresto\b', r'\btrino\b'],

    # AI/ML Tools
    "LLMs": [r'\bllm\b', r'\blarge\s+language\s+model\b'],
    "RAG": [r'\brag\b', r'\bretrieval\s+augmented\b'],
    "LangChain": [r'\blangchain\b'],
    "LlamaIndex": [r'\bllamaindex\b', r'\bllama[\s_-]?index\b'],
    "OpenAI API": [r'\bopenai\b', r'\bgpt-4\b', r'\bgpt-3\b'],
    "Anthropic API": [r'\banthropic\b', r'\bclaude\s+api\b'],
    "Embeddings": [r'\bembedding\b', r'\bvector\s+embedding\b'],
    "Vector Databases": [r'\bvector\s+database\b', r'\bpinecone\b', r'\bweaviate\b', r'\bchroma\b', r'\bqdrant\b', r'\bpgvector\b'],
    "Agents": [r'\bai\s+agent\b', r'\bagentic\b'],
    "Prompt Engineering": [r'\bprompt\s+engineer\b', r'\bprompting\b'],
    "TensorFlow": [r'\btensorflow\b', r'\btf\b'],
    "PyTorch": [r'\bpytorch\b', r'\btorch\b'],
    "Scikit-learn": [r'\bscikit[\s-]?learn\b', r'\bsklearn\b'],
    "Hugging Face": [r'\bhugging\s*face\b', r'\btransformers\s+library\b'],

    # Cloud
    "AWS": [r'\baws\b', r'\bamazon\s+web\s+services\b'],
    "Azure": [r'\bazure\b', r'\bmicrosoft\s+azure\b'],
    "GCP": [r'\bgcp\b', r'\bgoogle\s+cloud\b'],
    "Docker": [r'\bdocker\b'],
    "Kubernetes": [r'\bkubernetes\b', r'\bk8s\b'],
    "Terraform": [r'\bterraform\b'],
    "CI/CD": [r'\bci/cd\b', r'\bgithub\s+actions\b', r'\bjenkins\b', r'\bgitlab\s+ci\b'],

    # Databases
    "PostgreSQL": [r'\bpostgresql\b', r'\bpostgres\b'],
    "MySQL": [r'\bmysql\b'],
    "MongoDB": [r'\bmongodb\b', r'\bmongo\b'],
    "Redis": [r'\bredis\b'],
    "Elasticsearch": [r'\belasticsearch\b', r'\bopensearch\b'],
    "Cassandra": [r'\bcassandra\b'],

    # Frameworks
    "FastAPI": [r'\bfastapi\b'],
    "Django": [r'\bdjango\b'],
    "Flask": [r'\bflask\b'],
    "Spring Boot": [r'\bspring\s+boot\b'],
    "Node.js": [r'\bnode\.?js\b'],
    "React": [r'\breact\.?js\b', r'\breact\b'],
    "Celery": [r'\bcelery\b'],
}

# Required signal keywords
_REQUIRED_SIGNALS = re.compile(
    r'\b(required|must\s+have|must[\s-]have|mandatory|essential|minimum|you\s+have|you\s+bring|qualifications)\b',
    re.IGNORECASE
)
_PREFERRED_SIGNALS = re.compile(
    r'\b(preferred|nice[\s-]to[\s-]have|bonus|plus|desired|ideally|familiarity|knowledge\s+of)\b',
    re.IGNORECASE
)


@dataclass
class SkillExtraction:
    required_skills: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    all_skills: list[str] = field(default_factory=list)


def extract_skills(title: str, description: str) -> SkillExtraction:
    """Extract structured skills from job title and description."""
    combined = f"{title} {description}".lower()
    found: dict[str, float] = {}  # skill -> position (earlier = more likely required)

    for skill, patterns in SKILL_CATALOG.items():
        for pattern in patterns:
            m = re.search(pattern, combined, re.IGNORECASE)
            if m:
                found[skill] = m.start()
                break

    if not found:
        return SkillExtraction()

    # Split description into required/preferred sections
    desc_lower = description.lower()
    required_section_pos = -1
    preferred_section_pos = -1

    req_m = _REQUIRED_SIGNALS.search(desc_lower)
    pref_m = _PREFERRED_SIGNALS.search(desc_lower)

    if req_m:
        required_section_pos = req_m.start()
    if pref_m:
        preferred_section_pos = pref_m.start()

    required_skills: list[str] = []
    preferred_skills: list[str] = []

    for skill, pos in sorted(found.items(), key=lambda x: x[1]):
        if preferred_section_pos > 0 and pos >= preferred_section_pos:
            preferred_skills.append(skill)
        else:
            required_skills.append(skill)

    # ── Core-first ordering within each bucket ─────────────────────────────────
    # Skills that appear in _CORE_SKILLS float to the top; ties preserve
    # their original position-based order (stable sort).
    def _core_first(skill: str) -> int:
        return 0 if skill in _CORE_SKILLS else 1

    required_skills.sort(key=_core_first)
    preferred_skills.sort(key=_core_first)

    # ── Hard caps ──────────────────────────────────────────────────────────────
    required_skills  = required_skills[:_MAX_REQUIRED]
    preferred_skills = preferred_skills[:_MAX_PREFERRED]

    # all_skills = deduped union, core-first, then preferred extras
    seen: set[str] = set(required_skills)
    all_skills: list[str] = list(required_skills)
    for s in preferred_skills:
        if s not in seen:
            all_skills.append(s)
            seen.add(s)

    return SkillExtraction(
        required_skills=required_skills,
        preferred_skills=preferred_skills,
        all_skills=all_skills,
    )


def match_skills_to_resume(job_skills: list[str], resume_skills: list[str]) -> tuple[list[str], list[str]]:
    """Returns (matched, missing)."""
    resume_lower = {s.lower() for s in resume_skills}
    matched = [s for s in job_skills if s.lower() in resume_lower]
    missing = [s for s in job_skills if s.lower() not in resume_lower]
    return matched, missing
