"""
Domain normalization utilities.

Single source of truth for domain canonicalization.
Used by discovery, expander, and DB dedup checks.

Rules (applied in order):
  1. Strip scheme (https://, http://)
  2. Strip www.
  3. Strip known career/job subdomains
  4. Extract path-less hostname
  5. Lowercase + strip whitespace

Examples:
  careers.stripe.com    → stripe.com
  jobs.stripe.com       → stripe.com
  www.stripe.com        → stripe.com
  https://stripe.com/   → stripe.com
  boards.greenhouse.io  → NOT stripped (it's a vendor, not a company domain)
"""

from __future__ import annotations

import re

# Subdomains that are "career-page wrappers" — not the real root domain.
# Stripping them lets us unify  careers.acme.com  and  acme.com.
_CAREER_SUBDOMAINS: frozenset[str] = frozenset({
    "careers", "jobs", "job", "apply", "boards",
    "career", "work", "hiring", "talent", "recruit",
    "opportunities", "join", "team",
})

# Known ATS / third-party vendor domains — never strip subdomains from these
# because  boards.greenhouse.io  is NOT the same root as  greenhouse.io.
_VENDOR_DOMAINS: frozenset[str] = frozenset({
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "smartrecruiters.com",
    "myworkdayjobs.com",
    "icims.com",
    "taleo.net",
    "successfactors.com",
    "jobvite.com",
    "breezy.hr",
    "recruitee.com",
    "workable.com",
    "bamboohr.com",
    "rippling.com",
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
})


def normalize_domain(raw: str) -> str:
    """
    Return the canonical root domain for a company URL or domain string.
    Always returns a non-empty lowercase string; never raises.

    >>> normalize_domain("https://careers.stripe.com/openings")
    'stripe.com'
    >>> normalize_domain("jobs.lever.co")   # vendor — keep as-is
    'lever.co'
    >>> normalize_domain("www.grab.com")
    'grab.com'
    """
    if not raw:
        return ""

    domain = raw.strip().lower()

    # 1. Strip scheme
    domain = re.sub(r"^https?://", "", domain)

    # 2. Strip trailing path / query / fragment
    domain = domain.split("/")[0].split("?")[0].split("#")[0]

    # 3. Strip port
    domain = domain.split(":")[0]

    # 4. Strip www.
    if domain.startswith("www."):
        domain = domain[4:]

    # 5. Check if it's a vendor domain — if so, return bare vendor root
    for vendor in _VENDOR_DOMAINS:
        if domain == vendor or domain.endswith("." + vendor):
            return vendor

    # 6. Strip known career subdomains (only the leftmost label)
    parts = domain.split(".")
    if len(parts) > 2 and parts[0] in _CAREER_SUBDOMAINS:
        domain = ".".join(parts[1:])

    return domain or raw.strip().lower()


def root_name_from_domain(domain: str) -> str:
    """
    Extract the organisational name part from a domain.

    Examples:
      stripe.com  → stripe
      getdbt.com  → getdbt
      co.uk       → (empty — don't use two-label ccTLDs)
    """
    domain = normalize_domain(domain)
    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[0]
    return domain
