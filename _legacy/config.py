"""
Configuration for Job Automation System.

Loads settings from environment variables with validation and sensible defaults.
Copy .env.example to .env and fill in your values.
"""

import os
import sys
import logging
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# ─── Validation Helpers ─────────────────────────────────────────
def _get_env(key: str, default: str = "", required: bool = False) -> str:
    val = os.getenv(key, default)
    if required and not val:
        logger.critical(f"Required environment variable {key} is not set.")
        sys.exit(1)
    return val


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        logger.warning(f"Invalid int for {key}, using default {default}")
        return default


def _get_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        logger.warning(f"Invalid float for {key}, using default {default}")
        return default


def _get_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


# ─── AI Provider ────────────────────────────────────────────────
OPENAI_API_KEY = _get_env("OPENAI_API_KEY")
OPENAI_MODEL = _get_env("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TEMPERATURE_RESUME = _get_float("OPENAI_TEMPERATURE_RESUME", 0.3)
OPENAI_TEMPERATURE_COVER = _get_float("OPENAI_TEMPERATURE_COVER", 0.4)
OPENAI_MAX_TOKENS_RESUME = _get_int("OPENAI_MAX_TOKENS_RESUME", 3000)
OPENAI_MAX_TOKENS_COVER = _get_int("OPENAI_MAX_TOKENS_COVER", 1500)

# ─── Telegram Alerts ────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = _get_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _get_env("TELEGRAM_CHAT_ID")

# ─── Email Alerts ───────────────────────────────────────────────
EMAIL_ENABLED = _get_bool("EMAIL_ENABLED", False)
EMAIL_SMTP_HOST = _get_env("EMAIL_SMTP_HOST", "smtp.gmail.com")
EMAIL_SMTP_PORT = _get_int("EMAIL_SMTP_PORT", 587)
EMAIL_SENDER = _get_env("EMAIL_SENDER")
EMAIL_PASSWORD = _get_env("EMAIL_PASSWORD")
EMAIL_RECIPIENT = _get_env("EMAIL_RECIPIENT")

# ─── Database ───────────────────────────────────────────────────
DB_PATH = _get_env("DB_PATH", "data/jobs.db")

# ─── Fetching ───────────────────────────────────────────────────
MAX_CONCURRENCY = _get_int("MAX_CONCURRENCY", 20)
REQUEST_TIMEOUT = _get_int("REQUEST_TIMEOUT", 30)
MAX_RETRIES = _get_int("MAX_RETRIES", 3)
RATE_LIMIT_PER_SECOND = _get_float("RATE_LIMIT_PER_SECOND", 5)
CIRCUIT_BREAKER_THRESHOLD = _get_int("CIRCUIT_BREAKER_THRESHOLD", 5)
CIRCUIT_BREAKER_TIMEOUT = _get_int("CIRCUIT_BREAKER_TIMEOUT", 300)

# ─── Job fetch sources (comma-separated: greenhouse,lever,ashby,workday,indeed,linkedin) ─
def _parse_fetch_sources() -> frozenset[str]:
    raw = _get_env("JOB_FETCH_SOURCES", "greenhouse,lever,ashby").lower()
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


JOB_FETCH_SOURCES = _parse_fetch_sources()

# ─── Filtering (full-time DE / SWE family) ───────────────────────
TARGET_TITLES = [
    "data engineer",
    "software engineer",
    "senior data engineer",
    "senior software engineer",
    "staff data engineer",
    "staff software engineer",
    "principal data engineer",
    "principal software engineer",
    "backend engineer",
    "backend software engineer",
    "full stack engineer",
    "full-stack engineer",
    "fullstack engineer",
    "platform engineer",
    "data platform engineer",
    "analytics engineer",
    "etl engineer",
    "machine learning engineer",
    "ml engineer",
]

EXCLUDED_KEYWORDS = [
    "intern",
    "internship",
    "co-op",
    "part-time",
    "part time",
    "contract",
    "contractor",
    "freelance",
    "temporary",
    "volunteer",
    "unpaid",
    "per diem",
    "1099",
]

# Log new matches to console when Telegram/email unavailable
CONSOLE_NOTIFICATIONS = _get_bool("CONSOLE_NOTIFICATIONS", True)

PREFERRED_LOCATIONS = [
    kw.strip().lower()
    for kw in _get_env("PREFERRED_LOCATIONS", "remote,hybrid,san francisco,new york,seattle,austin").split(",")
    if kw.strip()
]

MIN_EXPERIENCE_YEARS = _get_int("MIN_EXPERIENCE_YEARS", 0)
MAX_EXPERIENCE_YEARS = _get_int("MAX_EXPERIENCE_YEARS", 99)

# ─── Resume ─────────────────────────────────────────────────────
MASTER_RESUME_PATH = _get_env("MASTER_RESUME_PATH", "data/master_resume.txt")
RESUME_OUTPUT_DIR = _get_env("RESUME_OUTPUT_DIR", "data/resumes")

# ─── Company Career Portals ─────────────────────────────────────

# Greenhouse boards (company board tokens)
GREENHOUSE_BOARDS = [
    "airbnb", "airtable", "asana", "brex", "canva", "chime", "coinbase",
    "databricks", "datadog", "discord", "doordash", "dropbox", "duolingo",
    "figma", "flexport", "gusto", "hashicorp", "hubspot", "instacart",
    "intercom", "klaviyo", "lattice", "linear", "lyft", "marqeta", "miro",
    "mongodb", "netlify", "notion", "nuro", "openai", "pagerduty",
    "palantir", "plaid", "postman", "ramp", "reddit", "retool", "rippling",
    "robinhood", "roku", "scale", "segment", "sentry", "snyk", "splunk",
    "squarespace", "stripe", "toast", "twilio", "vercel", "wealthfront",
    "webflow", "wiz", "zapier", "zendesk",
]

# Lever boards (company slugs — lowercase for API)
LEVER_BOARDS = [
    "netflix", "spotify", "cloudflare", "anduril", "anthropic", "databricks",
    "figma", "grammarly", "miro", "notion", "openai", "plaid", "reddit",
    "rippling", "scale", "snyk", "stripe", "vercel",
]

# Ashby boards (company slugs — Ashby's public job board API)
ASHBY_BOARDS = [
    "ramp", "notion", "linear", "vercel", "clerk", "resend", "raycast",
    "elevenlabs", "perplexity", "cursor",
]

# Workday tenants (company_name: tenant_url pattern)
# Format: "company_name:tenant_subdomain"
WORKDAY_TENANTS = [
    "amazon:amazon",
    "microsoft:microsoft",
    "google:google",
    "meta:meta",
    "apple:apple",
    "salesforce:salesforce",
    "adobe:adobe",
    "oracle:oracle",
    "uber:uber",
    "nvidia:nvidia",
]

# Indeed search queries (keyword + optional location)
INDEED_SEARCHES = [
    {"query": "data engineer", "location": "remote"},
    {"query": "software engineer", "location": "remote"},
    {"query": "backend engineer", "location": "remote"},
    {"query": "ml engineer", "location": "remote"},
    {"query": "platform engineer", "location": "remote"},
]

# LinkedIn search parameters
LINKEDIN_ENABLED = _get_bool("LINKEDIN_ENABLED", False)
LINKEDIN_SEARCHES = [
    {"keywords": "data engineer", "location": "United States", "f_WT": "2"},  # 2=remote
    {"keywords": "software engineer", "location": "United States", "f_WT": "2"},
    {"keywords": "backend engineer", "location": "United States", "f_WT": "2"},
    {"keywords": "ml engineer", "location": "United States", "f_WT": "2"},
]

# ─── Scoring Weights ───────────────────────────────────────────
SCORE_WEIGHT_TITLE = _get_float("SCORE_WEIGHT_TITLE", 30.0)
SCORE_WEIGHT_SKILLS = _get_float("SCORE_WEIGHT_SKILLS", 35.0)
SCORE_WEIGHT_EXPERIENCE = _get_float("SCORE_WEIGHT_EXPERIENCE", 15.0)
SCORE_WEIGHT_LOCATION = _get_float("SCORE_WEIGHT_LOCATION", 10.0)
SCORE_WEIGHT_PERKS = _get_float("SCORE_WEIGHT_PERKS", 10.0)

# ─── Dashboard ──────────────────────────────────────────────────
DASHBOARD_PAGE_SIZE = _get_int("DASHBOARD_PAGE_SIZE", 50)
DASHBOARD_THEME = _get_env("DASHBOARD_THEME", "light")  # "light" or "dark"


# ─── Validation on import ───────────────────────────────────────
def validate_config():
    """Log warnings for missing optional configs."""
    warnings = []
    if not OPENAI_API_KEY:
        warnings.append("OPENAI_API_KEY not set — resumes use placeholder text until configured")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        warnings.append("Telegram credentials not set — alerts disabled")
    if EMAIL_ENABLED and (not EMAIL_SENDER or not EMAIL_PASSWORD):
        warnings.append("EMAIL_ENABLED=true but sender/password not set")
    if not os.path.exists(MASTER_RESUME_PATH):
        warnings.append(f"Master resume not found at {MASTER_RESUME_PATH}")
    for w in warnings:
        logger.warning(f"[Config] {w}")
    return warnings


# Run validation on import (non-blocking)
_config_warnings = validate_config()
