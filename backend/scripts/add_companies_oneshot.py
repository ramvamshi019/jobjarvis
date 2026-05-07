"""
Overnight company adder. Probes ~10,000 curated company slugs across 9 ATS
platforms and upserts confirmed hits directly via asyncpg. Bypasses Celery
entirely.

Resumable: progress is checkpointed to /tmp/companies_checkpoint.json after
each platform finishes. If the run is interrupted (Ctrl+C, container restart,
network blip), just re-run the same command and it picks up where it left off.

Run inside the celery_worker container (has httpx + asyncpg):
  docker cp backend/scripts/add_companies_oneshot.py \\
      jobjarvis_celery_worker:/tmp/add_companies.py
  docker exec jobjarvis_celery_worker python3 -u /tmp/add_companies.py

Expected runtime: 2–4 hours. Expected new companies: 4,000–8,000.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import asyncpg

DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://jobjarvis:jobjarvis@postgres:5432/jobjarvis",
).replace("postgresql+asyncpg://", "postgresql://")

PROBE_TIMEOUT = 8
USER_AGENT = "JobJarvis/1.0 ramvamshikrishna0@gmail.com"
CHECKPOINT_FILE = Path("/tmp/companies_checkpoint.json")


# ─── PROBE FUNCTIONS ─────────────────────────────────────────────────────────

async def probe_greenhouse(client, slug):
    try:
        r = await client.get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            timeout=PROBE_TIMEOUT,
        )
        return r.status_code == 200
    except Exception:
        return False


async def probe_lever(client, slug):
    try:
        r = await client.get(
            f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1",
            timeout=PROBE_TIMEOUT,
        )
        return r.status_code == 200 and isinstance(r.json(), list)
    except Exception:
        return False


async def probe_ashby(client, slug):
    try:
        r = await client.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
            timeout=PROBE_TIMEOUT,
        )
        return r.status_code == 200
    except Exception:
        return False


async def probe_smartrecruiters(client, slug):
    try:
        r = await client.get(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            params={"limit": 1},
            timeout=PROBE_TIMEOUT,
        )
        return r.status_code == 200
    except Exception:
        return False


async def probe_workable(client, slug):
    try:
        r = await client.post(
            f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",
            json={"limit": 1, "details": False},
            headers={"Content-Type": "application/json"},
            timeout=PROBE_TIMEOUT,
        )
        return r.status_code == 200
    except Exception:
        return False


async def probe_bamboohr(client, slug):
    try:
        r = await client.get(
            f"https://{slug}.bamboohr.com/jobs/embed2.php",
            timeout=PROBE_TIMEOUT,
        )
        return r.status_code == 200 and len(r.text) > 500
    except Exception:
        return False


async def probe_teamtailor(client, slug):
    try:
        r = await client.get(
            f"https://{slug}.teamtailor.com/jobs.json",
            timeout=PROBE_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        if r.status_code != 200:
            return False
        data = r.json()
        return isinstance(data, (dict, list))
    except Exception:
        return False


async def probe_recruitee(client, slug):
    try:
        r = await client.get(
            f"https://{slug}.recruitee.com/api/offers/?scope=published&limit=1",
            timeout=PROBE_TIMEOUT,
        )
        return r.status_code == 200
    except Exception:
        return False


async def probe_icims(client, slug):
    try:
        r = await client.get(
            f"https://{slug}.icims.com/jobs/intro",
            timeout=PROBE_TIMEOUT,
        )
        return r.status_code in (200, 301, 302)
    except Exception:
        return False


# Per-platform config: (name, probe_fn, careers_url_template, priority, concurrency)
PLATFORMS = [
    ("greenhouse",      probe_greenhouse,      "https://boards.greenhouse.io/{slug}",        70, 60),
    ("lever",           probe_lever,           "https://jobs.lever.co/{slug}",               70, 40),
    ("ashby",           probe_ashby,           "https://jobs.ashbyhq.com/{slug}",            65, 30),
    ("smartrecruiters", probe_smartrecruiters, "https://careers.smartrecruiters.com/{slug}", 60, 40),
    ("workable",        probe_workable,        "https://apply.workable.com/{slug}",          55, 5),
    ("bamboohr",        probe_bamboohr,        "https://{slug}.bamboohr.com/careers",        50, 15),
    ("teamtailor",      probe_teamtailor,      "https://{slug}.teamtailor.com/jobs",         65, 20),
    ("recruitee",       probe_recruitee,       "https://{slug}.recruitee.com",               50, 15),
    ("icims",           probe_icims,           "https://{slug}.icims.com/jobs/intro",        55, 15),
]


# ─── DB UPSERT ───────────────────────────────────────────────────────────────

def _slug_to_name(slug):
    import re
    return " ".join(w.capitalize() for w in re.split(r"[-_\.]+", slug) if w)


async def upsert_company(conn, ats, slug, careers_url_tpl, priority):
    name = _slug_to_name(slug)
    now = datetime.now(timezone.utc)
    next_scan = now + timedelta(minutes=30)
    careers_url = careers_url_tpl.format(slug=slug)
    try:
        await conn.execute(
            """
            INSERT INTO companies (
                name, ats, ats_identifier, careers_url,
                priority_score, scan_priority,
                scan_frequency_minutes, next_scan_at, active,
                failure_count, consecutive_failures, jobs_found_count,
                created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5::integer, $5::double precision, $6, $7, true, 0, 0, 0, $8, $8)
            ON CONFLICT (name) DO UPDATE SET
                priority_score = GREATEST(companies.priority_score, EXCLUDED.priority_score),
                scan_priority  = GREATEST(companies.scan_priority,  EXCLUDED.scan_priority),
                active = true,
                updated_at = EXCLUDED.updated_at
            """,
            name, ats, slug, careers_url, priority, 360,
            next_scan, now,
        )
        return True
    except Exception as e:
        # Don't spam — just count failures
        return False


# ─── CHECKPOINT ──────────────────────────────────────────────────────────────

def load_checkpoint():
    if not CHECKPOINT_FILE.exists():
        return {}
    try:
        return json.loads(CHECKPOINT_FILE.read_text())
    except Exception:
        return {}


def save_checkpoint(state):
    try:
        CHECKPOINT_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


# ─── MAIN ────────────────────────────────────────────────────────────────────

async def main():
    print(f"Connecting to {DB_DSN.split('@')[-1]}…", flush=True)
    conn = await asyncpg.connect(DB_DSN)

    # Dedup the slug list once
    unique_slugs = sorted(set(SLUGS))
    print(f"Loaded {len(unique_slugs)} unique slugs (from {len(SLUGS)} entries)", flush=True)
    print(f"Probing across {len(PLATFORMS)} ATS platforms\n", flush=True)

    state = load_checkpoint()
    if state:
        done = [k for k, v in state.items() if v.get("done")]
        if done:
            print(f"Resuming — already done: {', '.join(done)}\n", flush=True)

    grand_total_hits = sum(
        v.get("hits", 0) for v in state.values() if v.get("done")
    )
    grand_total_upserted = sum(
        v.get("upserted", 0) for v in state.values() if v.get("done")
    )

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=PROBE_TIMEOUT,
    ) as client:
        for ats, probe_fn, url_tpl, priority, concurrency in PLATFORMS:
            if state.get(ats, {}).get("done"):
                print(f"[{ats}] already done (hits={state[ats]['hits']} "
                      f"upserted={state[ats]['upserted']}) — skipping", flush=True)
                continue

            print(f"[{ats}] probing {len(unique_slugs)} slugs "
                  f"(concurrency={concurrency})…", flush=True)
            sem = asyncio.Semaphore(concurrency)
            done_count = 0
            confirmed = []

            async def probe_one(slug):
                async with sem:
                    return slug, await probe_fn(client, slug)

            tasks = [probe_one(s) for s in unique_slugs]
            for fut in asyncio.as_completed(tasks):
                slug, ok = await fut
                done_count += 1
                if ok:
                    confirmed.append(slug)
                if done_count % 500 == 0:
                    print(f"  [{ats}] {done_count}/{len(unique_slugs)} probed, "
                          f"{len(confirmed)} confirmed so far…", flush=True)

            print(f"[{ats}] confirmed {len(confirmed)} / {len(unique_slugs)}", flush=True)

            upserted = 0
            errors = 0
            for slug in confirmed:
                if await upsert_company(conn, ats, slug, url_tpl, priority):
                    upserted += 1
                else:
                    errors += 1
            print(f"[{ats}] upserted {upserted}  errors {errors}", flush=True)

            state[ats] = {"done": True, "hits": len(confirmed), "upserted": upserted}
            save_checkpoint(state)
            grand_total_hits += len(confirmed)
            grand_total_upserted += upserted

            # Quick total snapshot from DB
            try:
                row = await conn.fetchrow(
                    "SELECT COUNT(*) AS n FROM companies WHERE active=true"
                )
                print(f"[{ats}] DB total active companies: {row['n']}\n", flush=True)
            except Exception:
                pass

    await conn.close()
    print("=" * 60, flush=True)
    print("FINAL SUMMARY", flush=True)
    print("=" * 60, flush=True)
    for ats in [p[0] for p in PLATFORMS]:
        v = state.get(ats, {})
        print(f"  {ats:18s} hits={v.get('hits', 0):5d}  "
              f"upserted={v.get('upserted', 0):5d}", flush=True)
    print(f"  {'TOTAL':18s} hits={grand_total_hits:5d}  "
          f"upserted={grand_total_upserted:5d}", flush=True)


# ─── SLUG LIST ───────────────────────────────────────────────────────────────
# ~10,000 curated company slugs, cross-probed against every ATS platform.
# Sources: YC W05–S25, Forbes Cloud 100/AI 50, Inc 5000, Fortune 500/1000,
# Wellfound directories, public ATS board crawls, sector-specific company
# directories, common SaaS/fintech/healthtech/devtools naming patterns.

SLUGS: list[str] = [
    # ── AI / ML / Foundation Models ────────────────────────────────────────
    "openai", "anthropic", "cohere", "mistral", "mistral-ai", "perplexity",
    "perplexity-ai", "scale", "scale-ai", "huggingface", "hugging-face",
    "stability-ai", "stabilityai", "runway", "runwayml", "midjourney",
    "elevenlabs", "eleven-labs", "character-ai", "characterai", "inflection",
    "inflection-ai", "adept", "adept-ai", "imbue", "pika", "pika-labs",
    "together", "together-ai", "togetherai", "replicate", "fireworks-ai",
    "fireworks", "anyscale", "modal", "modal-labs", "lambda", "lambda-labs",
    "lambda-labs-inc", "coreweave", "groq", "cerebras", "samba-nova",
    "sambanova", "graphcore", "tenstorrent", "rain-ai", "etched", "tinygrad",
    "weights-and-biases", "weights-biases", "wandb", "comet-ml", "cometml",
    "neptune-ai", "neptune", "arize", "arize-ai", "fiddler", "fiddler-ai",
    "aporia", "whylabs", "tecton", "feast", "hopsworks", "dbt-labs", "dbt",
    "elementary", "elementary-data", "monte-carlo", "montecarlo",
    "great-expectations", "soda", "soda-core", "soda-data", "alation", "atlan",
    "select-star", "selectstar", "metaphor", "metaphor-data", "collibra",
    "informatica", "glean", "glean-work", "guru", "getguru", "mem", "mem-ai",
    "dust", "dust-tt", "writer", "writer-ai", "jasper", "jasper-ai", "copy-ai",
    "copyai", "anyword", "leonardo", "leonardo-ai", "ideogram", "synthesia",
    "d-id", "didgeneration", "heygen", "hey-gen", "tavus", "captions",
    "captions-ai", "descript", "krisp", "krisp-ai", "otter", "otter-ai",
    "fireflies", "fireflies-ai", "tldv", "grain", "grain-co", "fathom",
    "fathom-video", "supernormal", "spinach", "spinach-ai", "read-ai", "readai",
    "circleback", "tactiq", "rev", "rev-com", "verbit", "verbit-ai",
    "speechify", "play-ht", "playht", "resemble", "resemble-ai", "murf",
    "murf-ai", "wellsaid-labs", "wellsaid", "papercup", "deepl",
    "unstructured", "unstructured-io", "llamaindex", "langchain", "langchain-ai",
    "chroma", "chroma-core", "weaviate", "qdrant", "pinecone", "vectara",
    "zilliz", "milvus", "marqo", "lancedb", "deeplake", "activeloop",
    "exa", "exa-ai", "tavily", "you", "you-com", "kagi", "phind",
    "harvey", "harvey-ai", "spellbook", "lawgeex", "evisort", "ironclad",
    "magic", "magic-dev", "factory", "factory-ai", "cursor", "cursor-ai",
    "cursor-so", "codeium", "tabnine", "sourcegraph", "cody", "supermaven",
    "augment", "augment-code", "continue", "continue-dev", "windsurf",
    "all-hands", "openhands", "devin", "cognition", "cognition-ai",
    "lindy", "lindy-ai", "decagon", "decagon-ai", "sierra", "sierra-ai",
    "rasa", "rasa-hq", "snorkel", "snorkel-ai", "labelbox", "labelstudio",
    "label-studio", "humanloop", "humanloop-ai", "promptlayer", "lakera",
    "lakera-ai", "voyage", "voyage-ai", "voyageai", "nomic", "nomic-ai",
    "letta", "memgpt", "browser-use", "browserbase", "stagehand", "exa-labs",
    "fixie", "fixie-ai", "deepgram", "assembly-ai", "assemblyai",
    "speechmatics", "rev-ai", "soniox", "gladia", "predibase", "outerbounds",
    "metaflow", "comet", "encord", "pachyderm", "iterative", "iterative-ai",
    "modular", "modular-ai", "mosaicml", "mosaic", "mosaic-ml", "octoml",
    "octo-ml", "deci", "deci-ai", "foundry", "foundry-ai", "harmonic",
    "harmonic-ai", "kura", "kura-ai", "outset", "outset-ai", "regal", "regal-io",
    "fal", "fal-ai", "haiper", "haiper-ai", "luma", "luma-ai", "luma-labs",
    "viggle", "vidu", "kling", "minimax", "01ai", "01-ai", "yi", "qwen",
    "deepseek", "moonshot", "kimi", "x-ai", "xai", "stability", "ai21",
    "ai21-labs", "aleph-alpha", "alephalpha", "lightonai", "light-on",
    "patronus", "patronus-ai", "ragas", "comet-llm", "langfuse", "lang-fuse",
    "portkey", "portkey-ai", "arize-phoenix", "guardrails", "guardrails-ai",
    "rebuff", "deepchecks", "evidently", "evidently-ai", "trulens",

    # ── Fintech / Payments / Banking / Wealth / Insurance ──────────────────
    "stripe", "plaid", "brex", "ramp", "mercury", "carta", "pulley",
    "deel", "remote", "oyster", "rippling", "gusto", "justworks",
    "papaya-global", "velocity-global", "globalization-partners",
    "atlas", "remote-com", "deel-com",
    "affirm", "klarna", "afterpay", "sezzle", "perpay", "zip-co", "zip",
    "sunbit", "uplift", "kueski", "tabby", "tamara", "spotii", "postpay",
    "chime", "cash-app", "cashapp", "varo", "dave", "current", "current-mobile",
    "step", "greenlight", "monzo", "starling", "starling-bank", "n26",
    "revolut", "wise", "transferwise", "remitly", "rapyd", "airwallex",
    "currencyfair", "wirebarley", "sendwave", "world-remit", "worldremit",
    "tabapay", "moov", "moov-financial", "modern-treasury", "marqeta",
    "lithic", "highnote", "synctera", "treasury-prime", "unit", "unit-co",
    "alloy", "persona", "onfido", "veriff", "trulioo", "socure", "middesk",
    "sardine", "sift", "unit21", "fingerprint", "fingerprintjs", "incode",
    "jumio", "au10tix", "iddata", "idnow",
    "yodlee", "mx-technologies", "mx", "finicity", "tink", "salt-edge",
    "saltedge", "truelayer", "true-layer", "akoya", "atomic-fi", "atomic",
    "increase", "fragment", "treasure", "treasure-financial", "rho",
    "novo", "found", "found-tax", "relay", "relay-fi", "lili", "bluevine",
    "kabbage", "fundbox", "ondeck", "lendio", "biz2credit", "lend-up",
    "tala", "branch-international", "creditas", "konfio", "clip", "ualah",
    "nubank", "nu", "neon", "c6", "inter", "banco-inter", "stone",
    "pagseguro", "rappipay", "rappi", "ualabia", "tribal", "tribal-credit",
    "pomelo", "pomelo-co", "jeeves", "stori", "stori-card",
    "blend", "blend-labs", "better", "better-com", "loansnap", "lower",
    "rocketmortgage", "rocket-mortgage", "lendingclub", "lending-club",
    "upstart", "prosper", "sofi", "sofi-technologies", "wealthfront",
    "betterment", "personal-capital", "ellevest", "stash", "acorns",
    "robinhood", "public-com", "public", "etoro", "moomoo", "webull",
    "futu", "interactive-brokers", "schwab", "fidelity", "vanguard",
    "blackrock", "two-sigma", "twosigma", "citadel", "citadel-securities",
    "jane-street", "janestreet", "hudson-river-trading", "hrt", "drw",
    "tower-research", "imc-trading", "imc", "optiver", "flow-traders",
    "akuna-capital", "akuna", "deshaw", "de-shaw", "millennium", "balyasny",
    "point72", "renaissance", "renaissance-technologies",
    "lemonade", "metromile", "root", "root-insurance", "hippo", "kin",
    "kin-insurance", "branch", "branch-insurance", "next-insurance", "pie",
    "pie-insurance", "embroker", "vouch", "vouch-insurance", "newfront",
    "marshmallow", "by-miles", "qoala", "policygenius", "thezebra",
    "the-zebra", "compare", "compare-com", "insurify", "ladder", "ladder-life",
    "ethos", "ethos-life", "haven-life", "havenlife", "fabric", "fabric-life",
    "republic", "republic-co", "wefunder", "seedinvest", "seedrs", "crowdcube",
    "fundrise", "yieldstreet", "masterworks", "rally", "rally-rd", "vint",
    "alto", "alto-ira", "rocket-dollar", "trustco", "ira-financial",
    "vyzer", "kubera", "betterment-financial", "facet-wealth", "facet",
    "personalcapital", "wealthfront-inc", "vise", "altruist", "altruist-corp",
    "tradingview", "tradier", "alpaca", "alpaca-markets", "polygon-io",
    "polygonio", "iexcloud", "iex-cloud", "questrade", "wealthsimple",
    "qonto", "shine", "memo-bank", "anytime", "lydia", "max", "max-bank",
    "spendesk", "pennylane", "swile", "alan", "payfit", "moss", "circula",
    "yokoy", "monite",

    # ── Devtools / Cloud / Infra / Observability / Databases ───────────────
    "github", "gitlab", "bitbucket", "atlassian", "jetbrains", "sourcegraph",
    "replit", "codesandbox", "gitpod", "stackblitz", "stack-blitz",
    "raycast", "warp", "warp-dev", "tabby", "wave-terminal", "fig", "fig-io",
    "linear", "linear-app", "height", "height-app", "shortcut", "shortcut-com",
    "clickup", "asana", "monday", "trello", "jira", "confluence", "notion",
    "coda", "almanac", "remnote", "obsidian", "logseq", "roam", "roam-research",
    "tana", "anytype",
    "dropbox", "box", "egnyte", "sync", "tresorit", "internxt", "filebase",
    "storj", "storj-labs", "filecoin", "protocol-labs",
    "cloudflare", "fastly", "akamai", "netlify", "vercel", "render", "railway",
    "fly", "fly-io", "porter", "porter-sh", "qovery", "northflank",
    "digitalocean", "linode", "vultr", "scaleway", "hetzner", "ovhcloud",
    "ovh", "upcloud", "kamatera",
    "datadog", "newrelic", "new-relic", "dynatrace", "splunk", "grafana",
    "grafana-labs", "honeycomb", "honeycomb-io", "lightstep", "chronosphere",
    "chronosphere-io", "logz", "logz-io", "sumologic", "sumo-logic",
    "papertrail", "scalyr", "elastic", "elasticsearch", "elasticsearch-co",
    "opensearch", "humio", "victoriametrics", "victoria-metrics", "axiom",
    "axiom-co", "betterstack", "better-stack", "uptime-com", "pingdom",
    "uptimerobot", "statuspage", "instatus", "ohstat", "freshping",
    "checkly", "checklyhq", "thousandeyes",
    "pagerduty", "incident-io", "rootly", "blameless", "firehydrant",
    "fire-hydrant", "transparent", "incident", "squadcast", "opsgenie",
    "victorops", "spike-sh", "bigpanda", "moogsoft", "zenduty",
    "sentry", "rollbar", "bugsnag", "raygun", "airbrake", "trackjs",
    "logrocket", "log-rocket", "sessionstack", "fullstory", "full-story",
    "hotjar", "smartlook", "mouseflow", "contentsquare",
    "snyk", "checkmarx", "veracode", "sonarqube", "sonarsource", "codacy",
    "deepsource", "deep-source", "semgrep", "snyk-io", "fossa", "blackduck",
    "synopsys", "whitesource", "mend", "mend-io", "nexus", "sonatype",
    "anchore", "aqua-security", "aquasec", "twistlock", "stackrox",
    "lacework", "orca-security", "wiz-io", "wiz", "panther", "panther-labs",
    "hunters", "exabeam", "securiti", "securiti-ai", "darktrace", "vectra",
    "vectra-ai", "extrahop", "expel",
    "crowdstrike", "sentinelone", "cylance", "cybereason", "carbon-black",
    "carbonblack", "fireeye", "trellix", "fortinet", "paloalto-networks",
    "palo-alto", "checkpoint", "check-point", "trend-micro", "trendmicro",
    "kaspersky", "eset", "bitdefender", "malwarebytes", "norton", "mcafee",
    "tanium", "netskope", "zscaler", "z-scaler", "armis", "armis-security",
    "claroty", "nozomi", "nozomi-networks", "dragos", "dragos-com",
    "ordr", "ordr-net", "axonius", "axonius-jobs", "rumble", "runzero",
    "automox", "kandji", "kandji-io", "jamf", "intune", "ms-intune",
    "manageengine", "manage-engine", "mosyle", "vmware-workspaceone",
    "stackhawk", "stack-hawk", "noname-security", "noname", "salt-security",
    "saltsecurity", "akto", "akto-io", "wallarm", "data-theorem",
    "ghost-security", "promon", "appsealing", "approov", "approov-io",
    "abnormal", "abnormal-security", "knowbe4", "knowbe-4", "cofense",
    "ironscales", "valimail", "agari", "vade-secure", "barracuda",
    "sophos", "webroot", "f-secure", "withsecure",
    "okta", "auth0", "ping", "ping-identity", "onelogin", "jumpcloud",
    "duo", "duo-security", "yubico", "rsa", "rsa-security", "cyberark",
    "beyondtrust", "sailpoint", "saviynt", "delinea", "thycotic", "centrify",
    "qualys", "tenable", "rapid7", "secureworks", "trustwave",
    "stytch", "stytch-com", "magic-auth", "magic-link-auth", "frontegg",
    "front-egg", "supertokens", "super-tokens", "fusionauth", "fusion-auth",
    "ory", "ory-cloud", "keycloak", "loginradius", "login-radius",
    "circleci", "circleci-com", "buildkite", "harness", "harness-io",
    "codefresh", "spinnaker", "argoproj", "argo-cd", "argocd", "fluxcd",
    "tekton", "drone", "drone-io", "semaphore", "semaphoreci", "travis",
    "travis-ci", "appveyor", "azure-devops", "github-actions", "gitlab-ci",
    "pulumi", "env0", "spacelift", "terrateam", "atlantis", "scalr",
    "terraformcloud", "hashicorp", "terraform", "vault", "consul", "nomad",
    "boundary", "waypoint",
    "postman", "insomnia", "httpie", "hoppscotch", "thunder-client",
    "stoplight", "stoplight-io", "swagger", "swaggerhub", "redocly", "redoc",
    "readme", "readme-io", "mintlify", "fern", "fern-api", "scalar",
    "doppler", "infisical", "akeyless", "akeyless-security", "1password",
    "1password-business", "lastpass", "dashlane", "bitwarden", "keeper",
    "keeper-security", "psono", "passbolt",
    "ngrok", "tailscale", "tailscale-com", "netbird", "twingate", "twingate-com",
    "perimeter81", "perimeter-81", "zerotier", "wireguard", "openvpn",
    "expressvpn", "nordvpn", "nord-security", "surfshark", "protonvpn",
    "hasura", "hasura-io", "prisma", "prisma-io", "planetscale", "neon-tech",
    "fauna", "fauna-db", "supabase", "supabase-com", "appwrite", "nhost",
    "directus", "strapi", "sanity", "sanity-io", "contentful", "storyblok",
    "prismic", "prismic-io", "webiny", "ghost-org", "ghost",
    "redis", "redis-labs", "redis-com", "couchbase", "couchdb", "datastax",
    "hazelcast", "memcached", "mongodb", "mongo", "mongo-db", "documentdb",
    "cockroachdb", "cockroach-labs", "cockroachlabs", "yugabyte", "yugabyte-db",
    "tigerbeetle", "scylla", "scylladb", "scylla-db", "tikv", "vitess",
    "tidbcloud", "tidb", "pingcap", "ping-cap", "starrocks", "doris",
    "clickhouse", "clickhouse-com", "tinybird", "tinybird-co", "rockset",
    "materialize", "materialize-io", "imply", "imply-io", "druid", "pinot",
    "starburst", "starburst-data", "trino", "presto", "dremio", "ahana",
    "snowflake", "databricks", "google-bigquery", "bigquery", "redshift",
    "amazon-redshift", "synapse",
    "fivetran", "airbyte", "stitch", "matillion", "rivery", "hevo", "hevo-data",
    "hightouch", "census", "rudderstack", "rudder-stack", "segment", "mparticle",
    "tealium", "treasure-data", "treasuredata", "amplitude", "mixpanel",
    "heap", "fullstory-data", "june", "june-so", "pendo", "userpilot",
    "intercom-product", "appcues", "chameleon", "userflow", "user-flow",
    "loops", "loops-so", "knock", "knock-app", "courier", "courier-com",
    "trycourier", "novu", "novu-co",
    "temporal", "temporal-io", "orkes", "conductor-io", "uber-cadence",
    "n8n", "n8n-io", "make", "make-com", "zapier", "ifttt", "tray", "tray-io",
    "workato", "automate-io", "boomi", "snaplogic", "mulesoft", "talend",
    "informatica-cloud", "alteryx", "knime", "data-iku", "dataiku",
    "datarobot", "data-robot", "h2o-ai", "h2o", "domino", "domino-ai",
    "domino-data-lab", "iguazio", "valohai", "verta", "verta-ai",
    "tilt", "tilt-dev", "devspace", "garden-io", "garden-tools", "skaffold",
    "okteto", "earthly", "earthly-build", "dagger", "dagger-io",
    "buf", "buf-build",
    "retool", "appsmith", "budibase", "tooljet", "tooljet-com", "lowdefy",
    "internal-io", "airplane", "airplane-dev", "interval", "interval-com",
    "motor", "motor-admin",
    "jfrog", "artifactory", "harbor", "quay", "github-container-registry",
    "docker", "docker-inc", "docker-com", "rancher", "rancher-labs",
    "suse", "openshift", "redhat", "red-hat", "vmware", "tanzu", "weaveworks",
    "weave", "lens", "mirantis", "platform9", "platform-nine", "kubeflow",
    "kubeshop", "kubespray",

    # ── SaaS B2B / CRM / Sales / Marketing ─────────────────────────────────
    "salesforce", "hubspot", "zoho", "freshworks", "freshdesk", "freshsales",
    "freshchat", "zendesk", "intercom", "drift", "qualified", "chili-piper",
    "chilipiper", "calendly", "savvycal", "savvy-cal", "youcanbookme",
    "doodle", "cal-com", "cal", "motion", "usemotion", "reclaim", "reclaim-ai",
    "akiflow", "sunsama", "amie", "amie-so", "vimcal", "fantastical",
    "todoist", "any-do", "anydo", "ticktick", "tick-tick", "things",
    "omnifocus",
    "asana-com", "monday-com", "wrike", "smartsheet", "basecamp", "teamwork",
    "teamwork-com", "podio", "freedcamp", "redbooth", "trello-com",
    "salesloft", "outreach", "outreach-io", "groove", "groove-co",
    "apollo", "apollo-io", "lemlist", "lem-list", "instantly", "instantly-ai",
    "smartlead", "smart-lead", "reply", "reply-io", "yesware", "mailshake",
    "klenty", "klenty-com", "saleshandy", "salesblink", "woodpecker",
    "woodpecker-co", "expandi",
    "gong", "chorus", "chorus-ai", "clari", "people-ai", "highspot",
    "seismic", "showpad", "mindtickle", "saleshood", "uberflip",
    "lattice", "engagement", "leandata", "lean-data", "lusha", "zoominfo",
    "zoom-info", "lead411", "rocketreach", "rocket-reach", "kaspr",
    "cognism", "leadiq", "lead-iq", "uplead", "snovio", "hunter", "hunter-io",
    "voilanorbert", "neverbounce", "never-bounce", "zerobounce", "zero-bounce",
    "millionverifier", "anymailfinder",
    "pipedrive", "close", "close-com", "copper", "copper-com", "insightly",
    "nutshell", "less-annoying-crm", "monday-sales-crm", "salesmate",
    "kommo", "amocrm", "amo-crm", "vtiger", "vtiger-com", "agile-crm",
    "agilecrm", "engagebay", "engage-bay", "noCRM-io", "nocrm",
    "mailchimp", "klaviyo", "attentive", "postscript", "sendgrid", "mailgun",
    "sparkpost", "sendinblue", "brevo", "constant-contact", "campaign-monitor",
    "activecampaign", "active-campaign", "drip", "drip-co", "convertkit",
    "convert-kit", "kit", "moosend", "getresponse", "get-response",
    "aweber", "iContact", "icontact", "benchmark-email",
    "braze", "iterable", "customerio", "customer-io", "leanplum", "moengage",
    "mo-engage", "clevertap", "clever-tap", "appboy", "swrve", "kahuna",
    "marketo", "eloqua", "pardot", "act-on", "acton-software",
    "tidio", "crisp", "crisp-chat", "olark", "freshchat-io", "livechat",
    "live-chat", "chatra", "tawk", "tawk-to", "manychat", "many-chat",
    "chatfuel", "landbot", "land-bot", "ada", "ada-support", "ultimate",
    "ultimate-ai", "kustomer", "gladly", "gorgias", "front", "front-app",
    "frontapp", "missive", "missive-app", "spike", "shift", "shift-com",
    "superhuman", "hey", "hey-com",
    "loop-email", "smtp2go", "mandrill", "mailjet",
    "twilio", "bandwidth", "vonage", "messagebird", "message-bird", "telnyx",
    "plivo", "infobip", "sinch", "clickatell", "syniverse", "kaleyra",
    "tyntec", "messente", "imimobile",
    "docusign", "pandadoc", "panda-doc", "proposify", "hellosign", "hello-sign",
    "dropbox-sign", "signnow", "sign-now", "adobesign", "adobe-sign", "qwilr",
    "proposable", "better-proposals", "betterproposals", "scribe", "scribe-how",
    "scribehow", "loom", "vidyard", "wistia", "vimeo", "brightcove", "kaltura",
    "panopto", "warpwire", "vooplayer",
    "ironclad", "spotdraft", "contractbook", "juro", "agiloft", "concord",
    "concord-now", "linksquares", "link-squares", "lexion", "lexion-ai",
    "icertis", "sirion", "sirion-labs", "conga",
    "tableau", "looker", "domo", "qlik", "thoughtspot", "sigma",
    "sigma-computing", "preset", "preset-io", "lightdash", "lightdash-com",
    "metabase", "metabase-com", "redash", "superset", "apache-superset",
    "mode", "mode-analytics", "modeanalytics", "hex", "hex-tech", "deepnote",
    "noteable", "noteable-io", "observable", "observable-hq", "snowsight",
    "metaplane", "metaplane-com", "datafold", "datafold-com", "cube",
    "cube-dev", "cube-js",
    "figma", "sketch", "adobe-xd", "framer", "invision", "invision-app",
    "marvel", "marvel-app", "balsamiq", "axure", "lucidchart", "lucid-chart",
    "miro", "mural", "whimsical", "drawio", "draw-io", "excalidraw",
    "tldraw", "tl-draw", "creately", "diagrams-net", "gliffy",
    "productboard", "product-board", "productplan", "product-plan", "aha",
    "aha-io", "roadmunk", "road-munk", "craft", "craft-io", "airfocus",
    "air-focus", "delibr", "savio", "canny", "canny-io", "uservoice",
    "user-voice", "upvoty", "frill", "frill-co",
    "launchdarkly", "launch-darkly", "split", "split-io", "statsig", "growthbook",
    "growth-book", "flagsmith", "configcat", "config-cat", "unleash",
    "get-unleash", "rollout", "optimizely", "convert", "vwo", "ab-tasty",
    "abtasty", "kameleoon", "monetate", "dynamic-yield",
    "slack", "microsoft-teams", "google-chat", "discord", "mattermost",
    "rocket-chat", "rocketchat", "element", "element-hq", "matrix-org",
    "wire", "wire-com", "threema", "signal", "telegram", "wickr",
    "zoom", "google-meet", "webex", "blue-jeans", "bluejeans", "ringcentral",
    "ring-central", "vonage-business", "8x8", "dialpad", "aircall", "talkdesk",
    "five9", "genesys", "nice-incontact", "niceincontact",

    # ── HR-tech / Recruiting ───────────────────────────────────────────────
    "lattice-com", "culture-amp", "cultureamp", "leapsome", "15five",
    "15-five", "betterworks", "reflektive", "engagedly", "officevibe",
    "office-vibe", "tinypulse", "tiny-pulse", "peakon", "peoplehum",
    "kazoo", "bonusly", "perkbox", "fond", "fond-hr", "blueboard", "guusto",
    "greenhouse", "greenhouse-software", "lever", "lever-co", "ashby",
    "workable", "workable-co", "breezy", "breezy-hr", "breezyhr", "jobvite",
    "icims", "smartrecruiters", "bamboohr", "bamboo-hr", "personio",
    "personio-com", "factorial", "factorial-hr", "humaans", "humaans-io",
    "hibob", "hi-bob", "bob", "charlie", "charlie-hr", "charliehr",
    "people", "people-hr", "peopleforce", "people-force", "freshteam",
    "fresh-team", "kissflow-hr", "zoho-people", "kekahr", "keka", "keka-hr",
    "darwinbox", "darwin-box", "qandle", "sumhr", "greythr", "grey-hr",
    "spine", "saral-design",
    "paychex", "adp", "paycom", "paylocity", "ultipro", "ukg", "kronos",
    "ceridian", "dayforce", "namely", "zenefits", "trinet", "insperity",
    "checkr", "sterling", "hireright", "first-advantage", "accurate",
    "accurate-bg", "goodhire", "good-hire", "shareable", "ebi", "ebi-com",
    "edge-information",
    "docebo", "cornerstone", "cornerstoneondemand", "skillsoft", "degreed",
    "axonify", "litmos", "talentlms", "talent-lms", "absorblms", "absorb-lms",
    "thinkific-edu", "teachable-edu", "skilljar", "northpass", "north-pass",
    "lessonly", "lesson-ly", "trainual", "guidde", "guidde-co", "tango",
    "tango-us", "tango-app", "userlane", "user-lane", "whatfix", "what-fix",
    "walkme", "walk-me", "spekit", "spek-it",
    "karat", "hackerrank", "hacker-rank", "codility", "codesignal",
    "code-signal", "coderpad", "coder-pad", "coderbyte", "leetcode",
    "interviewing-io", "interviewing", "triplebyte", "triple-byte", "topal",
    "toptal", "turing", "turing-com", "andela", "moonlight", "x-team",
    "xteam", "scalable-path", "scalable", "gun-io", "gunio",
    "fiverr", "upwork", "freelancer", "people-per-hour", "peopleperhour",
    "99designs", "designcrowd", "design-crowd",
    "modernloop", "modern-loop", "metaview", "metaview-ai", "vouch-talent",
    "covey", "dover", "dover-com", "fetcher", "fetcher-ai", "honeit",
    "interseller", "screenloop", "screen-loop", "willo", "willo-video",
    "spark-hire", "sparkhire", "videointerviews", "hireflix", "vervoe",
    "harqen", "modern-hire", "modernhire", "hirevue", "hire-vue",
    "criteria-corp", "criteria",

    # ── Healthtech / Biotech / Pharma ──────────────────────────────────────
    "hims", "hims-hers", "ro", "ro-co", "noom", "calibrate", "found-health",
    "everlywell", "everly-well", "lemonaid", "wisp", "nurx", "lola",
    "thirty-madison", "30-madison", "keeps", "musely", "rory", "wellinks",
    "babyscripts", "maven", "maven-clinic", "tia", "tiawomen", "kindbody",
    "kind-body", "carrot", "carrot-fertility", "progyny", "stork-club",
    "rocket-rx", "alto-pharmacy", "capsule", "capsule-corp",
    "blink-health", "blinkhealth", "goodrx", "good-rx", "scriptdrop",
    "amazon-pharmacy", "express-scripts",
    "labcorp", "quest", "quest-diagnostics", "thorne", "function",
    "function-health", "insidetracker", "inside-tracker", "levels",
    "levels-health", "lumen", "lumen-me", "ten-thousand", "tonal",
    "peloton", "echelon", "tempo", "tempo-fit", "mirror", "future",
    "future-fit", "freeletics", "fitbod", "fit-bod", "centr", "centr-health",
    "obe", "open-fit", "openfit", "alo-moves", "alomoves",
    "headspace", "calm", "calm-com", "ten-percent-happier", "tenpercent",
    "balance", "balance-app", "wim-hof", "insight-timer", "insighttimer",
    "smiling-mind", "happify", "happi-fy", "moodpath", "moodfit",
    "youper", "youper-com", "wysa", "woebot", "woebot-health", "talkspace",
    "talk-space", "betterhelp", "better-help", "ginger", "ginger-io",
    "modern-health", "modernhealth", "lyra", "lyra-health", "lyrahealth",
    "spring", "spring-health", "springhealth", "brightline", "bright-line",
    "headway", "headway-co", "alma", "helloalma", "hello-alma", "octave",
    "octavehealth", "two-chairs", "twochairs", "rula", "rula-health",
    "veeva", "medidata", "iqvia", "syneos-health", "icon-plc", "parexel",
    "pra-health", "prahealth", "covance", "ppd", "premier-research",
    "medpace", "everest-clinical", "syneos",
    "epic", "epic-systems", "cerner", "athenahealth", "athena-health",
    "allscripts", "veradigm", "nextgen-healthcare", "nextgen", "ehr",
    "drchrono", "dr-chrono", "kareo", "tebra", "ehealth", "practice-fusion",
    "elation", "elation-health", "carbon-health", "carbonhealth",
    "one-medical", "onemedical", "forward", "forward-health", "forward-com",
    "parsley-health", "parsleyhealth", "galileo", "galileo-health",
    "98point6", "98-point-6", "k-health", "khealth", "ada-health",
    "babylon", "babylon-health", "doctolib", "doctolib-com",
    "teladoc", "teladoc-health", "amwell", "mdlive", "doctor-on-demand",
    "doctorondemand", "plushcare", "plush-care", "sesame", "sesame-care",
    "well-app", "wellbe",
    "flatiron", "flatiron-health", "tempus", "tempus-labs", "syapse",
    "foundation-medicine", "foundationmedicine", "guardant", "guardant-health",
    "veracyte", "exact-sciences", "exactsciences", "natera", "natera-com",
    "myriad", "myriad-genetics", "invitae", "invitae-corp", "color",
    "color-health", "colorgenomics", "ancestry", "ancestrydna", "23andme",
    "helix", "helix-genomics",
    "10x-genomics", "pacbio", "oxford-nanopore", "illumina", "biontech",
    "moderna", "novavax", "regeneron", "biogen", "vertex",
    "vertex-pharmaceuticals", "alnylam", "bluebird", "bluebird-bio",
    "beam", "beam-therapeutics", "prime", "prime-medicine", "arc",
    "arc-institute", "crispr", "crispr-therapeutics", "intellia", "editas",
    "editas-medicine", "recursion", "recursion-pharmaceuticals", "insilico",
    "insilico-medicine", "schrodinger", "atomwise", "atomic-ai", "absci",
    "absci-bio", "deep-genomics", "deepgenomics", "exscientia", "benevolentai",
    "benevolent", "iktos", "valence", "valence-discovery", "isomorphic",
    "isomorphic-labs", "iambic", "iambic-therapeutics", "generate-biomedicines",
    "generate-bio", "dnanexus", "dna-nexus", "benchling", "synthego", "twist",
    "twist-bioscience", "ginkgo", "ginkgo-bioworks", "zymergen", "amyris",
    "perfect-day", "perfectday", "impossible-foods", "impossible", "beyond-meat",
    "beyondmeat", "eat-just", "eatjust", "memphis-meats", "upside-foods",
    "upsidefoods", "good-meat", "future-meat", "wildtype", "blue-nalu",
    "bluenalu",
    "modernizing-medicine", "modmed", "ambra-health", "definitive-healthcare",
    "definitive-hc", "olive", "olive-ai", "innovaccer", "qventus", "qgenda",
    "kyruus", "phreesia", "weave", "weavecomm", "wellsky", "well-sky",
    "pointclickcare", "matrixcare", "matrix-care", "homecare-homebase",
    "axxess", "axxess-com", "doximity", "dox-imity",
    "providence", "kaiser-permanente", "intermountain", "intermountain-healthcare",
    "geisinger", "northwell", "mass-general-brigham", "ucsf", "stanford-health",
    "cleveland-clinic", "mayo-clinic", "mount-sinai", "ny-presbyterian",
    "johns-hopkins", "uchicago-medicine", "michigan-medicine", "duke-health",
    "vanderbilt-health", "houston-methodist", "scripps", "sutter-health",
    "ssm-health", "trinity-health", "ascension", "tenet-healthcare",
    "rad-ai", "radai", "viz-ai", "vizai", "aidoc", "qure-ai", "qureai",
    "tempus-ai", "paige", "paige-ai", "paigeai", "ibex", "ibex-medical",
    "deep-bio", "subtle-medical", "behold-ai", "lunit", "lunit-io",
    "vuno", "siemens-healthineers", "ge-healthcare", "philips-healthcare",

    # ── E-commerce / D2C / Retail ──────────────────────────────────────────
    "shopify", "bigcommerce", "magento", "woocommerce", "wordpress", "wix",
    "squarespace", "weebly", "godaddy", "go-daddy", "duda",
    "shopify-plus", "shop-pay", "shop", "stripe-checkout",
    "amazon-seller-central", "amazon-fba", "shipbob", "shipstation",
    "easypost", "easy-post", "shippo", "endicia", "stamps-com",
    "pirateship", "pirate-ship", "pitney-bowes", "pitneybowes",
    "yotpo", "yotpo-loyalty", "stamped-io", "stamped", "okendo", "judge-me",
    "judgeme", "loox", "loox-reviews", "reviews-io", "trustpilot", "trust-pilot",
    "feefo", "shopperapproved", "shopper-approved", "kudobuzz", "powerreviews",
    "power-reviews", "bazaarvoice", "bazaar-voice",
    "rebuy", "smartrr", "smart-rr", "recharge", "re-charge", "bold-subscriptions",
    "bold-commerce", "subbly", "ordergroove", "order-groove", "loop-club",
    "skio", "stay-ai", "subscribe-pro",
    "loop", "loop-returns", "happy-returns", "happyreturns", "returnly",
    "narvar", "aftership", "after-ship", "shopify-returns", "returnfox",
    "returngo", "trytwo",
    "richpanel", "rich-panel", "delighted", "ask-nicely", "asknicely",
    "wonderment", "klar", "ortto", "ortto-com",
    "rebag", "the-realreal", "therealreal", "vestiaire", "vestiaire-collective",
    "depop", "poshmark", "mercari", "ebay", "etsy", "stockx", "goat",
    "stadium-goods", "rebag-com", "thredup", "tradesy", "kidizen",
    "warby-parker", "warby", "zenni", "zenni-optical", "felix-gray", "felixgray",
    "eyebuydirect", "eye-buy-direct", "glasses-com", "frames-direct",
    "framesdirect", "smartbuyglasses",
    "casper", "purple", "saatva", "tuft-and-needle", "tuftandneedle", "leesa",
    "helix-sleep", "helixsleep", "nectar", "dreamcloud", "dream-cloud", "puffy",
    "avocado", "avocado-mattress", "molecule", "molecule-mattress",
    "away", "away-travel", "monos", "monos-travel", "july", "july-co",
    "rimowa", "tumi", "samsonite", "briggs-and-riley", "patagonia", "rei",
    "rei-co-op", "outdoor-voices", "outdoorvoices", "tracksmith", "lululemon",
    "athleta", "alo-yoga", "aloyoga", "vuori", "vuori-clothing", "fabletics",
    "fabletics-co", "girlfriend", "girlfriend-collective", "spanx", "skims",
    "savage-x-fenty", "savage-fenty", "harvey-nichols", "harvey-nichols-com",
    "allbirds", "atoms", "rothys", "thursday-boots", "thursdayboots", "olukai",
    "feetures", "bombas", "stance", "darn-tough", "darntough", "smartwool",
    "icebreaker", "ice-breaker", "wool-and-prince", "woolandprince",
    "everlane", "everlane-co", "cuyana", "lo-and-sons", "loandsons", "mejuri",
    "mejuri-com", "monica-vinader", "monica-vinader-com", "missoma", "catbird",
    "ana-luisa", "analuisa", "kendra-scott", "kendra-scott-com", "stitch-fix",
    "stitchfix", "trunk-club", "the-black-tux", "blacktux", "bonobos",
    "indochino", "indo-chino", "suit-supply", "suitsupply", "rhone", "untuckit",
    "un-tuckit",
    "glossier", "glossier-com", "fenty-beauty", "fenty", "rare-beauty",
    "rarebeauty", "drunk-elephant", "drunkelephant", "tatcha", "the-ordinary",
    "deciem", "youth-to-the-people", "youthtothepeople", "summer-fridays",
    "summerfridays", "augustinus-bader", "skinceuticals", "paula-s-choice",
    "paulaschoice", "first-aid-beauty", "first-aid", "kiehls", "lush",
    "the-body-shop", "thebodyshop", "loccitane", "l-occitane", "aesop",
    "kosas", "ilia", "ilia-beauty", "merit", "merit-beauty", "westman-atelier",
    "westmanatelier", "rms-beauty", "rmsbeauty", "alleyoop", "alley-oop",
    "function-of-beauty", "functionofbeauty", "prose", "prose-com", "playa",
    "playa-beauty", "olaplex", "olap-lex", "amika", "amika-pro", "verb",
    "verb-products",
    "thrive-causemetics", "thrive-cosmetics", "thrivecosmetics", "morphe",
    "huda-beauty", "hudabeauty", "anastasia-beverly-hills", "anastasiabeverlyhills",
    "kylie-cosmetics", "kyliecosmetics", "kkw-beauty", "kkwbeauty", "stila",
    "tarte", "tartecosmetics", "becca", "beccacosmetics", "too-faced",
    "toofaced", "urban-decay", "urbandecay", "smashbox",
    "blue-bottle", "bluebottle", "stumptown", "stumptown-coffee", "intelligentsia",
    "la-colombe", "lacolombe", "verve", "verve-coffee", "philz", "philz-coffee",
    "third-wave-coffee", "thirdwave", "blue-tokai", "subko", "kapi",
    "tonx", "tonx-coffee", "trade-coffee", "tradecoffee", "atlas-coffee-club",
    "atlascoffee", "yes-plz", "yesplz", "driftaway", "drift-away",
    "death-wish-coffee", "deathwishcoffee", "wandering-bear", "wanderingbear",
    "high-brew", "highbrew", "chameleon-cold-brew", "chameleoncoldbrew",
    "stok", "stok-coffee",
    "soylent", "huel", "kachava", "ka-chava", "athletic-greens", "ag1",
    "feastables", "mr-beast-feastables", "magic-spoon", "magicspoon", "graza",
    "fly-by-jing", "flybyjing", "omsom", "wholesum", "kettle-fire", "kettlefire",
    "olipop", "poppi", "poppi-soda", "spindrift", "liquid-death", "liquiddeath",
    "celsius", "celsius-com", "alani-nu", "alaninu", "ghost-lifestyle",
    "ghostlifestyle", "honest-company", "honest", "the-honest-company",
    "honest-co", "burts-bees", "burtsbees", "tom-s-of-maine", "tomsofmaine",
    "method", "method-products", "mrs-meyers", "mrsmeyers", "seventh-generation",
    "seventhgeneration", "tide", "downy",
    "wayfair", "wayfair-com", "overstock", "houzz", "houzz-com", "bed-bath-beyond",
    "bedbathandbeyond", "bbby", "world-market", "worldmarket", "anthropologie",
    "urban-outfitters", "urbanoutfitters", "free-people", "freepeople",
    "madewell", "j-crew", "jcrew", "ann-taylor", "anntaylor", "loft", "loft-stores",
    "express", "express-com", "talbots", "talbots-com", "chicos", "chicos-fas",
    "white-house-black-market", "whitehouseblackmarket",
    "carvana", "vroom", "shift", "shift-cars", "carmax", "car-max", "carcomplaints",
    "carfax", "car-fax", "kelley-blue-book", "kbb", "edmunds", "edmunds-com",
    "autotrader", "auto-trader", "cars-com", "truecar", "true-car", "leasehackr",

    # ── Climate / Energy / Clean-tech ──────────────────────────────────────
    "watershed", "watershed-climate", "patch", "patch-io", "pachama", "sylvera",
    "cloverly", "isometric", "isometric-tech", "carbon-direct", "carbondirect",
    "stripe-frontier", "frontier-climate", "carbonfuture", "klima", "klima-app",
    "wren", "project-wren", "tomorrow", "tomorrow-bio", "ecologi", "ecologi-com",
    "joro", "klima-x", "treecard", "tree-card", "ourcarbon", "carbon-collective",
    "aspiration", "aspiration-com",
    "octopus-energy", "octopus", "octopusenergy", "bulb", "bulb-energy",
    "eon-next", "eonnext", "british-gas", "shell-energy", "ovo-energy", "ovo",
    "edf-energy", "edf", "iberdrola", "enel", "engie", "vattenfall", "uniper",
    "rwe", "ngrid", "national-grid",
    "tesla-energy", "sunrun", "sunpower", "sunnova", "vivint-solar", "tesla-solar",
    "enphase", "enphase-energy", "solaredge", "first-solar", "canadian-solar",
    "jinko-solar", "trina-solar", "longi", "ja-solar", "qcells", "hanwha-qcells",
    "aurora-solar", "auroras", "aurora-com", "energysage", "energy-sage",
    "palmetto", "palmetto-solar", "freedom-solar", "freedomsolar", "soligent",
    "solar-mosaic", "mosaic-solar", "loanpal", "goodleap", "good-leap",
    "ge-renewable", "siemens-gamesa", "vestas", "orsted",
    "form-energy", "formenergy", "ess-tech", "ambri", "natron", "natron-energy",
    "moxion", "moxion-power", "redwood-materials", "redwood", "redwoodmaterials",
    "ascend-elements", "ascendelements", "li-cycle", "licycle", "battery-resourcers",
    "northvolt", "freyr", "freyr-battery", "italvolt", "verkor", "automotive-cells",
    "abb-emobility", "abb", "blink-charging", "blinkcharging", "evgo",
    "chargepoint", "charge-point", "wallbox", "wallbox-com", "rewatt",
    "shell-recharge", "ionity", "ionity-net", "tesla-supercharger",
    "electrify-america", "electrifyamerica",
    "stem", "stem-inc", "fluence", "fluence-energy", "nextera", "nextera-energy",
    "duke-energy", "exelon", "dominion-energy", "southern-company", "pseg",
    "xcel-energy", "xcel", "evergy", "ppl-corporation", "ameren", "centerpoint",
    "centerpoint-energy", "consolidated-edison", "con-ed", "coned",
    "carbonchain", "carbonchain-com", "supplyshift", "supply-shift", "ecovadis",
    "eco-vadis", "ofs-portal", "openmep", "watershed-tech", "carbonplan",
    "carbon-plan", "climeworks", "carbfix", "sustaera", "verdox", "heirloom",
    "heirloom-carbon", "noya", "noya-co", "ebb-carbon", "ebbcarbon", "captura",
    "captura-corp", "vesta", "vesta-eco", "running-tide", "runningtide",
    "planetary", "planetary-tech", "planetary-technologies", "gigablue",
    "gigablue-co", "carbon-engineering", "global-thermostat", "twelve-co2",
    "twelve", "lanzatech", "lanza-tech", "solidia", "solidia-tech",
    "carbicrete", "carbon-cure", "carboncure", "fortera", "carbon-built",
    "carbonbuilt", "blue-planet", "calcarb", "ecocem",
    "form-bio", "formbio", "kobold", "kobold-metals", "earth-ai", "earthai",
    "kettle", "kettle-re", "demex", "raincoat", "tomorrow-io", "tomorrow-bio-co",
    "atmo", "atmo-ai", "salient", "salient-predictions", "weatheroptics",
    "weather-optics", "tomorrowio", "windward", "spire", "spire-global",
    "planet", "planet-labs", "planetary-labs", "iceye", "capella-space", "capella",
    "umbra", "umbra-space", "albedo", "albedo-space", "muon-space", "muonspace",
    "hubbleorgs", "hubble-network",

    # ── Real Estate / Proptech ─────────────────────────────────────────────
    "zillow", "redfin", "trulia", "realtor", "realtor-com", "homes-com",
    "compass", "side", "exp-realty", "exp", "fathom-realty", "fathomrealty",
    "real-brokerage", "the-real-brokerage", "homie", "homiecom", "houwzer",
    "redfin-now", "redfinnow", "opendoor", "offerpad", "knock", "knock-com",
    "homeward", "orchard", "flyhomes", "fly-homes", "homelight", "home-light",
    "rocket-homes", "guaranteed-rate", "guaranteedrate", "loandepot",
    "loan-depot", "freedom-mortgage", "caliber", "caliber-home-loans", "movement",
    "movement-mortgage", "fairway", "fairway-independent", "amerifirst",
    "ameri-first", "primelending", "prime-lending", "prosperity-home-mortgage",
    "matterport", "hover", "hover-co", "cubicasa", "cape-analytics", "cape",
    "vrbo", "homeaway", "airbnb-homes", "vacasa", "evolve", "evolve-vacation",
    "turnkey", "turn-key", "turnkey-vr", "casa-mia", "casamia", "kasa",
    "kasa-living", "blueground", "blue-ground", "sonder", "selina", "selina-com",
    "buildium", "appfolio", "entrata", "yardi", "yardi-systems", "realpage",
    "real-page", "rentmanager", "rent-manager", "rent-cafe", "rentcafe",
    "rezgo", "stays-net", "rentec-direct", "rentecdirect", "lessen", "mynd",
    "belong", "belong-home", "evernest", "doorvest", "door-vest", "roofstock",
    "roof-stock", "fundthatflip", "fund-that-flip", "groundfloor", "ground-floor",
    "peerstreet", "peer-street", "yieldstreet-real-estate", "cadre", "cadre-co",
    "compstak", "comp-stak", "moodys-analytics", "axiometrics", "rcanalytics",
    "rca", "real-capital-analytics", "yardi-matrix", "costar", "co-star",
    "loopnet", "loop-net", "crexi", "crexi-com", "buildout", "buildout-com",
    "ten-x", "tenx", "sharestates", "rentometer", "rent-o-meter", "rentcafe-leasing",
    "knock-crm", "knockcrm", "followupboss", "follow-up-boss", "sierra-interactive",
    "rethink-crm", "rethinkcrm", "wise-agent", "wiseagent", "lion-desk",
    "liondesk", "real-geeks", "realgeeks", "ylopo", "y-lopo", "boomtown",
    "boomtownroi", "chime-crm",
    "procore", "procoretech", "buildertrend", "builder-trend", "co-construct",
    "coconstruct", "buildxact", "build-xact", "houzz-pro", "thumbtack",
    "fieldwire", "field-wire", "plangrid", "plan-grid", "raken", "rakenapp",
    "rhumbix", "concrete-direct", "concretedirect", "buildot", "buildot-ai",
    "rabbet", "rabbet-com", "honest-buildings", "honestbuildings", "northspyre",
    "north-spyre", "bigfix-com",
    "katerra", "veev", "veev-homes", "abodu", "homefit", "vesper",

    # ── Logistics / Mobility / Freight ──────────────────────────────────────
    "uber", "lyft", "didi", "didi-chuxing", "ola", "ola-cabs", "grab", "grab-com",
    "gojek", "go-jek", "bolt", "bolt-eu", "wheely", "via", "via-transportation",
    "blacklane", "ride-share", "rapido", "namma-yatri", "ridey", "free-now",
    "freenow", "mytaxi", "ola-electric", "rivian", "lucid", "lucid-motors",
    "fisker", "fisker-inc", "polestar", "polestar-cars", "vinfast", "byd",
    "byd-auto", "nio", "xpeng", "li-auto", "li-xiang", "rivers", "lordstown",
    "lordstown-motors", "canoo", "arrival", "workhorse", "workhorse-group",
    "nikola", "nikolamotor", "hyzon", "hyzon-motors",
    "doordash", "uber-eats", "ubereats", "grubhub", "postmates", "deliveroo",
    "delivery-hero", "deliveryhero", "instacart", "shipt", "gopuff", "go-puff",
    "getir", "gorillas", "weezy", "jokr", "jokr-grocery", "fridge-no-more",
    "buyk", "1520", "rappi", "ifood", "i-food", "swiggy", "zomato", "blinkit",
    "zepto", "dunzo", "talabat", "careem", "noon", "wolt", "lieferando",
    "lieferheld", "just-eat", "just-eat-takeaway", "delivery-com", "doorstep",
    "chowbus", "chow-bus", "snackpass", "snack-pass",
    "convoy", "uber-freight", "uberfreight", "transfix", "freightos", "flexport",
    "flexport-jobs", "forto", "forto-com", "shipsy", "shipsy-tech", "rivigo",
    "blackbuck", "black-buck", "vahak", "kobo360", "kobo", "lori-systems",
    "lorisystems", "sennder", "sennder-com", "instafreight", "instafreight-com",
    "trelogix", "loadsmart", "load-smart", "freightos-jobs", "shipa-freight",
    "shipa", "iyno", "container-xchange", "container-x-change", "windward-ai",
    "vizion-co",
    "samsara", "motive", "motive-tech", "onetrack", "platform-science",
    "platformscience", "fleet-complete", "teletrac-navman", "telematics-com",
    "geotab", "geo-tab", "verizon-connect", "ramco", "trimble", "wialon",
    "fleetio", "fleet-io", "azuga", "lytx", "smartdrive", "samsara-jobs",
    "project44", "project-44", "fourkites", "four-kites", "shipwell",
    "ship-well", "uber-cargo",
    "stord", "saltbox", "flowspace", "flow-space", "flexe", "flexe-com",
    "veho", "roadie", "lalamove", "lala-move", "porter", "porter-india",
    "rivigo-india", "delhivery", "del-hivery", "bluedart", "blue-dart",
    "ekart", "shadowfax", "shadow-fax", "ecom-express",
    "shipbob-careers", "shipmonk", "ship-monk", "shipnetwork", "ship-network",
    "drive-now", "share-now", "sharenow", "blablacar", "bla-bla-car", "carma",
    "kyte", "kyte-cars", "fair", "fair-com", "kuhmute", "lime", "limebike",
    "lime-scooter", "bird", "bird-rides", "spin", "voi", "voi-technology",
    "tier", "tier-mobility", "dott", "dott-eu", "circ", "wind", "wind-mobility",
    "neuron", "neuron-mobility", "yulu", "vogo", "vogo-cab", "rapido-bike",
    "metroriders", "metro", "moovit", "transitapp", "transit-app", "citymapper",
    "city-mapper", "google-maps-jobs", "rome2rio", "rome-2-rio", "trainline",
    "train-line", "expedia", "kayak", "skyscanner", "hopper", "hopper-com",
    "tripadvisor", "trip-advisor", "tripaction", "tripactions", "navan",
    "navan-com", "spotnana", "trip-com", "trip", "cwt", "amex-gbt",
    "egencia", "concur", "concur-sap",

    # ── Edtech / Learning ──────────────────────────────────────────────────
    "duolingo", "coursera", "udemy", "skillshare", "masterclass", "brilliant",
    "khan-academy", "khanacademy", "byjus", "byju-s", "vedantu", "unacademy",
    "great-learning", "greatlearning", "scaler", "scaler-academy", "codingninjas",
    "coding-ninjas", "interviewbit", "interview-bit", "interviewing-io",
    "leetcode", "neetcode", "neet-code", "geeksforgeeks", "geeks-for-geeks",
    "hackerearth", "hacker-earth", "topcoder", "top-coder", "codeforces",
    "codechef", "code-chef", "exercism", "codewars", "kaggle", "datacamp",
    "data-camp", "dataquest", "data-quest", "365datascience", "365-data-science",
    "edureka", "intellipaat", "simplilearn", "simpli-learn", "udacity",
    "treehouse", "team-treehouse", "skillcrush", "skill-crush", "thinkful",
    "general-assembly", "generalassembly", "flatiron-school", "flatironschool",
    "hack-reactor", "hackreactor", "lambda-school", "bloomtech", "springboard",
    "spring-board", "thinkful-com", "fullstack-academy", "fullstackacademy",
    "app-academy", "appacademy", "le-wagon", "lewagon", "ironhack", "iron-hack",
    "wild-code-school", "wildcodeschool", "ada", "ada-developers-academy",
    "codeop", "code-op", "techwise", "tech-wise", "altcademy", "alt-cademy",
    "the-odin-project", "theodinproject", "freecodecamp", "free-code-camp",
    "edx", "futurelearn", "future-learn", "alison", "alison-com", "openlearn",
    "open-learn", "stanford-online", "harvard-online", "mit-open-learning",
    "mit-x-pro", "mitxpro", "harvard-extension", "google-skillshop",
    "skillshop", "google-classroom",
    "chegg", "quizlet", "course-hero", "coursehero", "studocu", "stu-docu",
    "scribd", "academia", "academia-edu", "researchgate", "research-gate",
    "varsity-tutors", "varsitytutors", "wyzant", "tutor-com", "preply",
    "italki", "i-talki", "verbling", "lingoda", "babbel", "rosetta-stone",
    "rosettastone", "memrise", "drops", "drops-app", "tandem-app", "tandem",
    "hello-talk", "hellotalk", "fluentu", "fluent-u", "lingvist", "anki",
    "anki-app", "supermemo",
    "outschool", "out-school", "white-hat-jr", "whitehatjr", "tynker",
    "code-org", "code-academy", "codecademy", "scrimba", "frontendmasters",
    "frontend-masters", "egghead", "egghead-io", "eggheadio", "pluralsight",
    "plural-sight", "linkedin-learning", "linkedinlearning", "lynda",
    "udemy-business", "udemybusiness",
    "newsela", "achieve3000", "khan-kids", "abcmouse", "abc-mouse", "homer",
    "learnwithhomer", "epic", "epic-kids", "lingokids", "lingo-kids",
    "duolingo-abc", "duo-abc", "elsa-speak", "elsa", "talking-tom",
    "outschool-com", "swing", "swing-edu", "swing-education", "edmentum",
    "ed-mentum", "stride", "k12", "stride-k12", "connections-academy",
    "primer", "primer-com", "modulo", "kahoot", "kahoot-com", "blooket",
    "quizizz", "quiz-izz", "nearpod", "near-pod", "padlet", "pad-let",
    "edpuzzle", "ed-puzzle", "flipgrid", "flip-grid", "schoology", "canvas",
    "instructure", "blackboard", "moodle",

    # ── Media / Content / Creator economy ──────────────────────────────────
    "spotify", "apple-music", "amazon-music", "tidal", "deezer", "pandora",
    "iheartradio", "iheart", "soundcloud", "sound-cloud", "audius", "bandcamp",
    "band-camp", "tunecore", "tune-core", "distrokid", "distro-kid", "cdbaby",
    "cd-baby", "songkick", "ticketmaster", "ticket-master", "stubhub",
    "vivid-seats", "vividseats", "seatgeek", "seat-geek", "axs", "etix",
    "eventbrite", "event-brite", "splash", "splash-that", "hopin", "hopin-jobs",
    "bizzabo", "biz-zabo", "cvent", "swapcard", "swap-card", "brella",
    "brella-network",
    "youtube", "vimeo", "twitch", "twitch-tv", "youtube-tv", "tiktok", "kuaishou",
    "douyin", "instagram", "snapchat", "pinterest", "reddit", "tumblr", "bluesky",
    "mastodon", "threads", "x", "twitter",
    "patreon", "buy-me-a-coffee", "buymeacoffee", "ko-fi", "kofi", "memberful",
    "substack", "ghost-org-pub", "beehiiv", "convertkit-creator", "kit-creator",
    "circle-so", "circle-com", "mighty-networks", "mightynetworks", "skool",
    "geneva", "geneva-app", "guild", "guildhq", "tribe", "tribe-so", "discourse",
    "vanilla", "vanilla-forums", "vanillaforums", "khoros", "spectrum-chat",
    "spectrum",
    "teachable", "thinkific", "kajabi", "podia", "mighty-pro", "mighty-co",
    "circle-creator", "kajabi-creators", "thinkific-com", "teachable-com",
    "mailerlite", "mailer-lite",
    "buzzfeed", "vox", "vox-media", "vice", "vice-media", "complex",
    "complex-networks", "dotdash", "dotdash-meredith", "hearst", "conde-nast",
    "condenast", "the-atlantic", "atlantic", "atlantic-media", "the-information",
    "information", "axios", "axios-hq", "semafor", "puck", "puck-news",
    "morning-brew", "morningbrew", "1440", "the-skimm", "skimm", "future-pub",
    "futureplc", "future-plc", "ziff-davis", "ziffdavis",
    "techcrunch", "tech-crunch", "the-verge", "theverge", "engadget",
    "ars-technica", "wired", "fast-company", "fastcompany", "forbes", "fortune",
    "bloomberg", "reuters", "wsj", "wall-street-journal", "ft", "financial-times",
    "the-economist", "economist", "nyt", "new-york-times", "nytimes",
    "washington-post", "washingtonpost", "guardian", "the-guardian", "bbc",
    "cnn", "msnbc", "fox-news", "foxnews", "abc-news", "abcnews", "nbc-news",
    "nbcnews", "cbs-news", "cbsnews",
    "stack-exchange", "stackexchange", "stack-overflow", "stackoverflow",
    "quora", "medium", "dev-to", "devto", "hashnode", "hash-node", "indiehackers",
    "indie-hackers", "producthunt", "product-hunt", "betalist", "beta-list",
    "lobsters", "lobster-rs", "bear-blog", "barnacle-blog",
    "behance", "dribbble", "deviantart", "deviant-art", "artstation",
    "art-station", "saatchi-art", "saatchiart", "society6", "society-6",
    "redbubble", "red-bubble", "etsy-art", "minted", "minted-com",
    "epidemic-sound", "epidemicsound", "artlist", "art-list", "soundstripe",
    "musicbed", "music-bed", "marmoset", "marmoset-music", "audiojungle",
    "audio-jungle", "envato", "envato-elements",

    # ── Gaming / Esports ───────────────────────────────────────────────────
    "epic-games", "riot-games", "blizzard", "activision", "ea", "electronic-arts",
    "ubisoft", "take-two", "rockstar-games", "rockstar", "bethesda", "id-software",
    "naughty-dog", "insomniac", "insomniac-games", "santa-monica-studio",
    "sony-santa-monica", "sony-playstation", "playstation", "xbox",
    "microsoft-xbox", "nintendo", "sega", "konami", "capcom", "square-enix",
    "namco-bandai", "bandai-namco", "sega-com", "atlus", "from-software",
    "fromsoftware", "fromsoft", "valve", "valve-software", "steam", "epic-store",
    "gog", "humble-bundle", "humble", "itch-io", "itchio", "kongregate",
    "rec-room", "recroom", "roblox", "minecraft", "mojang", "supercell",
    "supercell-jobs", "king", "king-com", "rovio", "miniclip", "miniclip-jobs",
    "playrix", "playtika", "scopely", "jam-city", "jamcity", "kabam", "wooga",
    "voodoo", "voodoo-io", "homa-games", "homa", "lion-studios", "lionstudios",
    "applovin", "iron-source", "ironsource", "tapjoy", "tap-joy", "vungle",
    "vungle-com", "adcolony", "ad-colony", "unity", "unity-technologies",
    "unity-com", "unreal", "unreal-engine", "epic-games-store", "godot",
    "godot-engine", "construct", "construct-3", "playcanvas", "play-canvas",
    "phaser", "phaser-io", "babylon-js", "babylon", "amazon-lumberyard",
    "lumberyard", "ggez",
    "vrchat", "vr-chat", "horizon-worlds", "horizonworlds", "spatial",
    "spatial-io", "altspacevr", "altspace", "engage-vr", "engage", "rumii",
    "anrk", "rumii-jobs",
    "100-thieves", "100thieves", "tsm", "team-solo-mid", "team-liquid",
    "fnatic", "fnatic-jobs", "g2-esports", "g2", "cloud9", "cloud-9",
    "envy-gaming", "envy", "evil-geniuses", "evilgeniuses", "complexity",
    "complexity-gaming", "faze-clan", "fazeclan", "fazethelivin",
    "nrg-esports", "nrg", "guild-esports", "vitality", "team-vitality",
    "natus-vincere", "navi", "navi-gg",

    # ── Cybersecurity (extra) ──────────────────────────────────────────────
    "transmit-security", "transmitsecurity", "tessian", "tessian-com",
    "material-security", "materialsecurity", "graphus", "vade",
    "intsights", "intsights-cyber", "shadow-protocol", "kelastic",
    "recorded-future", "recordedfuture", "flashpoint", "flashpoint-intel",
    "intel471", "intel-471", "anomali", "anomali-com", "threatconnect",
    "threat-connect", "imperva", "thales", "thales-cloud",

    # ── Defense / Aerospace / Space ─────────────────────────────────────────
    "spacex", "blue-origin", "rocket-lab", "rocketlab", "relativity-space",
    "relativityspace", "astra", "astra-com", "firefly-aerospace", "fireflyspace",
    "aevum", "aevum-aerospace", "abl-space", "abl-space-systems", "ursa-major",
    "ursamajor", "stoke-space", "stokespace", "vast-space", "vast", "varda",
    "varda-space", "axiom-space", "axiomspace", "voyager-space", "voyagerspace",
    "sierra-space", "sierraspace", "redwire", "redwire-space", "k2-space",
    "muon-space-systems", "true-anomaly", "trueanomaly", "anduril",
    "anduril-industries", "shield-ai", "shieldai", "saronic", "saronic-tech",
    "epirus", "epirus-defense", "skydio", "skydio-com", "iridium",
    "iridium-jobs", "viasat", "viasat-com", "intelsat", "intel-sat", "telesat",
    "telesat-com", "satellite-imaging", "boeing-jobs", "lockheed-jobs",
    "raytheon-jobs", "northrop-jobs", "general-dynamics-jobs", "ge-aerospace",
    "honeywell-aerospace", "rolls-royce", "rollsroyce", "rolls-royce-jobs",
    "pratt-whitney", "prattwhitney", "safran", "safran-group", "leonardo",
    "leonardo-spa", "thales-jobs", "bae-systems", "baesystems", "bae", "saab",
    "saab-defense", "kongsberg", "kongsberg-defense", "elbit", "elbit-systems",
    "rafael", "rafael-defense", "iai", "israel-aerospace", "indigo-defence",
    "embraer", "embraer-jobs", "bombardier", "bombardier-jobs", "dassault",
    "dassault-aviation", "dassault-systemes", "leonardo-aircraft",
    "lockheed-martin-skunk-works", "skunk-works",
    "palantir", "palantir-jobs", "primer", "primer-ai", "scale-defense",
    "ad-hoc", "ad-hoc-team", "anduril-jobs", "rebellion-defense",
    "rebelliondefense", "applied-intuition", "appliedintuition",
    "samsara-defense", "ghost-robotics", "ghostrobotics", "boston-dynamics",
    "bostondynamics", "agility-robotics", "agilityrobotics", "berkshire-grey",
    "berkshiregrey", "fetch-robotics", "fetchrobotics", "locus-robotics",
    "locusrobotics", "nuro", "nuro-com", "aurora", "aurora-innovation",
    "auroratech", "kodiak", "kodiak-robotics", "embark", "embark-trucks",
    "embarktrucks", "torc", "torc-robotics", "cruise", "cruise-llc", "waymo",
    "zoox", "argo-ai", "argoai", "motional", "motional-ad", "yandex-self-driving",
    "yandex", "didi-autonomous",

    # ── EU / UK / DACH / Nordics startups ──────────────────────────────────
    "spotify-eu", "klarna-eu", "skype", "criteo", "deezer-eu", "blablacar-eu",
    "doctolib-eu", "voodoo-eu", "ledger-eu", "exotec", "exotec-com", "ynsect",
    "y-nsect", "innovafeed", "agricool", "ledger-fr", "voodoo-fr", "akeneo",
    "akeneo-com", "contentsquare-eu", "criteo-eu", "blablacar-fr",
    "doctolib-fr",
    "n26-eu", "trade-republic", "traderepublic", "auxmoney", "celonis",
    "celonis-com", "personio-eu", "talentech", "talentech-com", "factorial-eu",
    "babbel-eu", "movinga", "delivery-hero-eu", "hellofresh", "hello-fresh",
    "hellofresh-com", "zalando", "zalando-eu", "about-you", "aboutyou",
    "lieferando-eu", "wundery", "raisin", "raisin-bank", "smava", "smava-de",
    "kontist", "kontist-de", "scalable-capital", "scalablecapital",
    "trade-republic-de", "wefox", "wefox-com", "getsafe", "get-safe", "clark",
    "clark-de", "ottonova", "snocks", "n26-de", "moss-com", "moss-financial",
    "memmingo", "yokoy-com", "personio-de", "deepl-jobs", "celonis-de",
    "konux", "konux-com", "agile-robots", "agilerobots", "isar-aerospace",
    "isaraerospace", "the-exploration-company", "explorationcompany",
    "auterion", "auterion-jobs", "atlassian-eu",
    "tomtom", "tom-tom", "booking", "booking-com", "bookingcom", "adyen",
    "adyen-jobs", "mollie", "mollie-com", "klarna-nl", "rocketreach-eu",
    "messagebird-jobs", "shapeways", "elastic-eu", "logz-io-eu", "snyk-eu",
    "fonoa", "fonoa-com", "ankorstore", "ankor-store", "mable-eu",
    "swile-jobs", "ankorstore-fr", "blacklane-eu", "babbel-jobs", "sennder-eu",
    "spotify-nordics", "klarna-stockholm", "voi-mobility", "tier-mobility-de",
    "klarna-se", "h-and-m", "hm", "ikea", "ikea-com", "lego", "lego-careers",
    "ericsson", "ericsson-jobs", "ericsson-com", "nokia", "nokia-jobs",
    "nokia-com", "skype-jobs", "spotify-jobs", "northvolt-eu", "einride",
    "einride-jobs", "polestar-jobs", "volvo", "volvocars", "volvo-cars",
    "volvo-trucks", "volvotrucks", "iziwork", "iziwork-fr", "aircall-fr",
    "voodoo-paris",
    "wise-jobs", "monzo-jobs", "starling-jobs", "revolut-jobs", "n26-jobs",
    "deliveroo-jobs", "transferwise-jobs", "trustpilot-jobs", "skyscanner-jobs",
    "checkout", "checkout-com", "checkoutcom", "wise-com",
    "babylon-jobs", "deepmind", "deep-mind", "mistral-paris", "synthesia-jobs",
    "elementai", "element-ai", "instadeep", "insta-deep", "wayve", "wayve-ai",
    "tractable", "tractable-ai", "graphcore-uk", "darktrace-jobs", "snyk-uk",
    "snowplow", "snowplow-analytics", "snowplowanalytics", "monitedeals",
    "improbable", "improbable-io", "octopus-energy-uk", "bulb-uk", "ovo-jobs",
    "spotify-london", "klarna-london", "google-london",
    "vinted", "vinted-com", "depop-uk", "vinted-jobs", "wolt-jobs",
    "supercell-jobs-fi", "rovio-jobs-fi", "spotify-stockholm",
    "skype-microsoft", "supercell-finland", "wolt-finland", "klarna-fi",

    # ── India / SE-Asia / APAC startups ────────────────────────────────────
    "flipkart", "myntra", "swiggy-jobs", "zomato-jobs", "ola-jobs", "uber-india",
    "uberindia", "paytm", "freecharge", "phonepe", "phone-pe", "googlepay-india",
    "razorpay", "razor-pay", "razorpay-jobs", "khatabook", "khata-book",
    "okcredit", "ok-credit", "vauld", "vault-finance", "stockal", "groww",
    "smallcase", "small-case", "scripbox", "scrip-box", "indmoney", "ind-money",
    "kuvera", "kuvera-com", "fyers", "fyers-securities", "vested",
    "vested-finance", "indwealth", "ind-wealth", "wealthy", "wealthy-in",
    "tickertape", "tickertape-in", "screener", "screener-in", "moneycontrol",
    "investing-com", "tradingview-yc",
    "zerodha", "groww-india", "upstox", "up-stox", "kite-zerodha",
    "smallcase-in", "indmoney-in", "fyers-india", "vested-india",
    "cred", "cred-club", "cred-app", "kuku-fm", "kukufm", "stage", "stage-in",
    "moj", "joshapp", "josh-app", "chingari", "shareit", "share-it", "truecaller",
    "true-caller", "intercom-india",
    "byju-s-india", "vedantu-india", "unacademy-india", "scaler-india",
    "physicswallah", "physics-wallah", "pw-skills", "pwskills", "great-learning-in",
    "talentsprint", "talent-sprint", "ugnext", "internshala", "intern-shala",
    "letsintern", "lets-intern", "naukri", "naukricom",
    "rebel-foods", "rebelfoods", "freshmenu", "fresh-menu", "fasoos", "faasos",
    "box8", "box-8", "ola-foods", "swiggy-instamart", "swiggy-mini",
    "blinkit-jobs", "zepto-jobs", "dunzo-jobs", "country-delight", "countrydelight",
    "milkbasket", "milk-basket", "bigbasket", "big-basket", "grofers",
    "grofers-jobs", "yulu-bikes", "vogo-jobs", "rapido-jobs", "metroprolific",
    "grab-jobs", "go-jek-jobs", "tokopedia", "shopee", "lazada", "qoo10",
    "carousell", "carouselljobs", "ninjavan", "ninja-van", "kargo", "kargo-tech",
    "akulaku", "ak-laku", "dana", "dana-id", "ovo-id", "gojek-id", "tokopedia-id",
    "bukalapak", "buka-lapak", "blibli", "tiket", "tiket-com", "traveloka",
    "agoda", "klook", "klook-jobs", "sea-limited", "sealtd", "shopback",
    "shop-back", "wego", "wego-com", "klook-singapore", "klook-com",
    "sky-mavis", "skymavis", "axie-infinity", "axieinfinity", "yield-guild-games",
    "yieldguildgames", "axie", "ronin",
    "atlassian-australia", "canva-jobs", "canva-au", "atlassian-au",
    "afterpay-au", "airwallex-au", "deputy-au", "deputy-com", "safetyculture",
    "safety-culture", "linktree", "link-tree", "spaceship", "spaceship-financial",
    "judo", "judo-bank", "up", "up-bank", "douugh", "doug-co",
    "tiktok-jobs", "bytedance", "byte-dance", "alibaba-jobs", "tencent-jobs",
    "baidu", "didi-jobs", "meituan", "meituan-jobs", "pinduoduo", "pdd",
    "xiaomi", "xiaomi-jobs", "huawei", "huawei-jobs", "lenovo", "lenovo-jobs",
    "oppo", "vivo", "realme", "oneplus", "one-plus",
    "kakao", "kakao-talk", "kakaotalk", "naver", "coupang", "coupangjobs",
    "krafton", "krafton-jobs", "ncsoft", "ncsoft-jobs", "nexon", "nexon-jobs",
    "smilegate", "smile-gate", "wemade", "we-made", "netmarble", "net-marble",
    "kakao-pay", "naver-pay", "toss", "toss-payments", "tosspayments",
    "viva-republica", "vivarepublica", "kakao-bank", "k-bank", "k-bank-co",

    # ── LatAm startups ──────────────────────────────────────────────────────
    "rappi-jobs", "kavak", "kavak-com", "merama", "valoreo", "wildlife-studios",
    "wildlifestudios", "loft", "loft-com", "quintoandar", "quinto-andar",
    "vivareal", "viva-real", "zap-imoveis", "zapimoveis", "kovi", "kovi-co",
    "yellow-cards", "yellowcard", "yellowfin", "loadsmart-latam", "rappi-mexico",
    "rappi-brasil", "ifood-jobs", "stone-co", "pagseguro-jobs", "ebanx",
    "e-banx", "kushki", "kushki-com", "ualah-co", "uala", "ualabia-co",
    "tribal-jobs", "creditas-jobs", "konfio-jobs", "xeneta-latam", "betterfly",
    "betterfly-com", "ualah-jobs", "hey-banco", "heybanco", "albo", "albo-mx",
    "albo-mexico", "klar-mx", "fondeadora", "fond-eadora", "dolado", "celcoin",
    "cel-coin", "creditas", "neon-banco",

    # ── Israel startups ─────────────────────────────────────────────────────
    "wix", "wix-com", "monday-com-israel", "fiverr", "fiverr-jobs",
    "lemonade-jobs", "playtika-jobs", "ironsource-jobs", "appsflyer-jobs",
    "applovin-jobs", "verbit-ai", "via-il", "moovit-jobs", "mobileye",
    "mobile-eye", "intel-mobileye", "rapyd-jobs", "redis-jobs", "torq",
    "torq-io", "snyk-jobs", "checkmarx-jobs", "wiz-jobs", "armis-jobs",
    "axonius-jobs", "claroty-jobs", "cyberreason-jobs", "cybereason-jobs",
    "transmit-security-jobs", "hibob-jobs", "papaya-global-jobs", "tipalti",
    "tipalti-jobs", "fundbox-jobs", "rapyd-il", "anchor", "anchor-il",
    "kaltura-jobs", "outbrain", "outbrain-jobs", "taboola", "taboola-jobs",
    "moonactive", "moon-active", "stoa", "stoa-school", "papaya-il",
    "tracetheory", "trax-retail", "trax", "anyvision", "any-vision", "oosto",
    "via-app", "gloat", "gloat-com", "fabric", "fabric-il", "trigo",
    "trigo-vision", "any-clip", "anyclip", "redislabs", "redis-com-jobs",
    "guardicore", "guardi-core", "imperva-jobs", "checkpoint-jobs",
    "cyberark-jobs", "varonis", "var-onis", "exabeam-jobs", "siemplify",
    "vulcan-cyber", "vulcan-cyber-io", "gcore", "g-core", "axonius-il",
    "spot-by-netapp", "spot-io", "datorios", "explorium", "explorium-ai",
    "lightrun", "light-run", "fundguard", "fund-guard", "intsig", "agora-il",
    "blue-vine-jobs",

    # ── Fortune 500 / Enterprise / Global Brands ───────────────────────────
    "apple", "amazon", "microsoft", "alphabet", "google", "meta", "facebook",
    "netflix", "tesla", "nvidia", "amd", "intel", "qualcomm", "broadcom",
    "marvell", "micron", "western-digital", "westerndigital", "seagate",
    "sk-hynix", "samsung", "samsung-electronics", "lg", "lg-electronics",
    "sony", "panasonic", "sharp", "toshiba", "kyocera", "fujitsu", "nec",
    "hp", "hp-inc", "hpe", "hewlett-packard", "dell", "dell-technologies",
    "lenovo-corp", "asus", "acer", "msi", "razer", "razer-com", "logitech",
    "corsair", "corsair-com", "steelseries", "steel-series", "hyperx",
    "oracle", "ibm", "salesforce-corp", "sap-jobs", "sap-com", "servicenow-jobs",
    "workday-corp", "intuit", "intuit-jobs", "autodesk", "autodesk-jobs",
    "ansys", "ptc", "ptc-com", "siemens", "siemens-software", "emerson",
    "honeywell-jobs", "rockwell", "rockwell-automation", "schneider",
    "schneider-electric", "abb-jobs", "ge", "general-electric", "ge-vernova",
    "general-motors", "gm-jobs", "ford-jobs", "ford-motor", "fordmotor",
    "stellantis", "fiat", "chrysler", "honda", "honda-jobs", "toyota",
    "toyota-jobs", "subaru", "mazda", "nissan", "infiniti", "mitsubishi",
    "kia", "hyundai", "hyundai-jobs", "audi", "bmw", "daimler", "mercedes",
    "mercedes-benz", "vw", "volkswagen", "porsche", "ferrari", "lamborghini",
    "maserati", "bentley", "rolls-royce-cars", "aston-martin", "astonmartin",
    "mclaren",
    "boeing-jobs-corp", "airbus", "airbus-jobs", "lockheed", "raytheon-corp",
    "northrop", "general-dynamics-corp", "leidos-corp", "saic-corp",
    "booz-allen-corp", "kbr", "engility",
    "jp-morgan", "jpmorgan", "jpmorgan-chase", "jpmc", "bank-of-america",
    "boa", "wells-fargo", "wellsfargo", "citi", "citigroup", "citibank",
    "goldman-sachs", "goldmansachs", "morgan-stanley", "morganstanley",
    "deutsche-bank", "deutschebank", "credit-suisse", "creditsuisse", "ubs",
    "barclays", "hsbc", "santander", "bbva", "bnp-paribas", "bnpparibas",
    "ing", "rabobank", "credit-agricole", "creditagricole", "lloyds", "natwest",
    "rbc", "td-bank", "td-securities", "scotiabank", "bmo", "cibc",
    "blackstone", "blackrock-jobs", "kkr", "carlyle", "apollo-management",
    "ares", "bain-capital", "tpg", "warburg-pincus", "warburgpincus",
    "general-atlantic", "generalatlantic", "vista-equity", "thoma-bravo",
    "thomabravo", "silver-lake", "silverlake", "advent", "advent-international",
    "permira", "cvc-capital", "kohlberg", "leonard-green", "leonardgreen",
    "providence-equity", "berkshire-partners", "berkshirepartners",
    "deloitte-jobs", "pwc-jobs", "kpmg-jobs", "ey-jobs", "accenture-jobs",
    "capgemini-jobs", "cognizant-jobs", "infosys-jobs", "wipro-jobs", "tcs-jobs",
    "tech-mahindra", "techmahindra", "hcl", "hcl-tech", "hcltech", "ltimindtree",
    "lti-mindtree", "mindtree", "persistent", "persistent-systems", "coforge",
    "hexaware", "mphasis", "birlasoft", "polaris-consulting", "polariscons",
    "zensar", "zensar-tech", "kpit", "kpit-tech", "happiest-minds",
    "happiestminds", "sasken", "tata-elxsi", "tataelxsi", "globant-jobs",
    "epam-jobs", "thoughtworks-jobs", "publicis", "publicis-sapient",
    "publicissapient", "wpp", "interpublic", "omnicom", "havas", "ddb",
    "bbdo", "ogilvy", "mccann", "leo-burnett", "leoburnett", "dentsu",
    "hakuhodo",
    "starbucks", "starbucks-jobs", "mcdonalds", "kfc", "yum", "yum-brands",
    "burger-king", "burger-king-jobs", "wendys", "chick-fil-a", "chickfila",
    "domino-s", "dominos", "papa-john-s", "papajohns", "subway", "subway-jobs",
    "jimmy-john-s", "jimmyjohns", "panera", "panerabread", "chipotle",
    "chipotle-jobs", "shake-shack", "shakeshack", "five-guys", "fiveguys",
    "in-n-out", "innout", "whataburger", "white-castle", "whitecastle",
    "raising-cane-s", "raisingcanes", "jollibee", "jollibee-foods", "tims",
    "tim-hortons", "timhortons", "dunkin", "dunkin-donuts", "krispy-kreme",
    "krispykreme",
    "marriott", "hilton", "hyatt", "wyndham", "ihg", "intercontinental-hotels",
    "accor", "accor-hotels", "best-western", "bestwestern", "choice-hotels",
    "choicehotels", "radisson", "kimpton", "kimpton-hotels", "four-seasons",
    "fourseasons", "ritz-carlton", "ritzcarlton", "st-regis", "stregis",
    "edition", "edition-hotels", "rosewood", "rosewood-hotels",
    "mandarin-oriental", "mandarinoriental", "shangri-la", "shangrila",
    "soho-house", "sohohouse",
    "delta", "delta-airlines", "deltaairlines", "united", "united-airlines",
    "unitedairlines", "american", "american-airlines", "americanairlines",
    "southwest", "southwest-airlines", "southwestairlines", "alaska-airlines",
    "alaskaairlines", "jetblue", "jet-blue", "spirit-airlines", "spiritairlines",
    "frontier-airlines", "frontierairlines", "allegiant", "allegiant-air",
    "allegiantair", "lufthansa", "british-airways", "britishairways",
    "air-france", "airfrance", "klm", "iberia", "iberia-airlines", "tap-portugal",
    "tapportugal", "emirates", "emirates-jobs", "qatar-airways", "qatarairways",
    "etihad", "etihad-airways", "singapore-airlines", "singaporeairlines",
    "ana", "ana-jobs", "jal", "japan-airlines", "japanairlines", "korean-air",
    "koreanair", "asiana", "cathay-pacific", "cathaypacific", "thai-airways",
    "thaiairways", "airindia", "air-india", "indigo", "indigo-airlines",
    "indigo-jobs", "fedex-jobs", "ups-jobs", "dhl", "dhl-jobs",
    "expeditors", "ch-robinson", "chrobinson", "xpo", "xpo-logistics",
    "knight-swift", "knightswift", "schneider-national", "schneidernational",
    "jb-hunt", "jbhunt", "yellow-corporation", "yellowcorp", "saia",
    "old-dominion", "olddominion", "estes-express", "estesexpress",
    "walmart", "walmart-corp", "target", "costco", "costco-jobs", "kroger",
    "kroger-jobs", "albertsons", "publix", "wegmans", "trader-joes", "traderjoes",
    "whole-foods", "wholefoods", "sprouts", "sprouts-jobs", "natural-grocers",
    "freshmarket", "fresh-market", "save-a-lot", "savealot", "aldi", "aldi-jobs",
    "lidl", "lidl-jobs", "ahold", "ahold-delhaize", "stop-and-shop",
    "stopandshop", "giant-food", "giantfood", "shoprite", "harris-teeter",
    "harristeeter", "winndixie", "winn-dixie", "h-e-b", "heb", "heb-grocery",
    "kohls", "kohls-jobs", "macys", "macys-inc", "nordstrom", "nordstrom-jobs",
    "neiman-marcus", "neimanmarcus", "saks", "saks-fifth-avenue",
    "saksfifthavenue", "bloomingdales", "tjx", "tjx-jobs", "tjmaxx", "marshalls",
    "homegoods", "ross", "ross-stores", "burlington", "five-below", "fivebelow",
    "dollar-general", "dollar-tree", "ollies", "ollies-bargain",
    "home-depot", "homedepot", "lowes", "menards", "ace-hardware", "acehardware",
    "true-value", "truevalue", "harbor-freight", "harborfreight", "tractor-supply",
    "tractorsupply", "rural-king", "ruralking", "northern-tool", "northerntool",
    "fleet-farm", "fleetfarm",
    "best-buy", "bestbuy", "gamestop", "game-stop", "barnes-and-noble",
    "barnesandnoble", "books-a-million", "booksamillion", "michaels", "hobby-lobby",
    "hobbylobby", "joann", "jo-ann", "michaels-jobs", "michaels-stores", "ulta",
    "ulta-beauty", "ultabeauty", "sephora", "sallybeauty", "sally-beauty",
    "cvs-health", "walgreens", "kaiser", "hca-healthcare", "hca",
    "tenethealthcare",
    "verizon", "att", "tmobile", "comcast", "spectrum", "charter", "cox",
    "altice", "frontier", "centurylink", "lumen", "windstream",
    "vodafone", "orange", "telefonica", "telstra", "optus", "rogers",
    "bell-canada", "bellcanada", "telus", "shaw", "videotron",
    "deutsche-telekom", "deutschetelekom", "kpn", "swisscom", "elisa", "tele2",
    "telenor", "telia", "tdc", "fastweb",
    "cisco", "cisco-jobs", "juniper", "juniper-networks", "junipernetworks",
    "arista", "arista-networks", "aristanetworks", "extreme-networks",
    "ciena", "infinera", "calix", "adtran", "ad-tran", "ribbon-communications",
    "ribbon-com", "f5", "f5-networks", "f5networks", "a10-networks",
    "a10networks",
    "exxonmobil", "chevron", "conocophillips", "shell", "bp", "totalenergies",
    "eni", "equinor", "repsol", "petrobras", "petrochina", "sinopec",
    "saudiaramco", "aramco", "adnoc", "qatarenergy", "kuwaitpetroleum",
    "petronas", "pemex", "pioneer-natural", "occidental", "oxy", "marathonoil",
    "devonenergy", "halliburton", "schlumberger", "slb", "bakerhughes",
    "weatherford", "nov", "nationaloilwell", "transocean", "valero",
    "phillips66", "marathon", "hess", "anadarko", "apache", "ncrcorp",
    "pfizer", "merck", "abbvie", "eli-lilly", "lilly", "bristol-myers",
    "bms", "amgen", "novartis", "roche", "sanofi", "gsk", "astrazeneca",
    "boehringer", "boehringer-ingelheim", "takeda", "daiichisankyo", "otsuka",
    "astellas", "novonordisk", "lundbeck",
    "tysonfoods", "smithfield", "jbs", "perduefarms", "pilgrims", "conagra",
    "kellogg", "kraftheinz", "kraft-heinz", "generalmills", "general-mills",
    "post", "postcorp", "campbells", "campbellsoup", "mondelez",
    "mondelez-international", "pepsico", "cocacola", "kdp", "keurig",
    "drpepper", "danone", "nestle", "unilever", "kimberly-clark",
    "kimberlyclark", "georgia-pacific", "gp", "weyerhaeuser", "international-paper",
    "ip", "westrock", "smurfit", "domtar",
    "procterandgamble", "pg", "colgate", "colgate-palmolive", "estee-lauder",
    "esteelauder", "revlon", "coty",

    # ── Insurance / Insurtech (extra) ──────────────────────────────────────
    "lemonade-insurance", "root-insurance-jobs", "metromile-jobs",
    "hippo-insurance", "kin-insurance-jobs", "branch-insurance-jobs",
    "newfront-insurance", "zego", "zego-insurance", "wefox-insurance",
    "ladderlife", "ethoslife", "havenlife", "fabriclife", "policygenius-jobs",
    "selectquote", "select-quote", "policy-bazaar", "policybazaar", "acko",
    "acko-com", "digit-insurance", "digitinsurance", "go-digit", "godigit",
    "coverfox", "cover-fox", "renewbuy", "renew-buy", "turtlemint",
    "turtle-mint", "insurancedekho", "insurance-dekho",
    "boost-insurance", "boostinsurance", "trov", "trov-insurance", "wrisk",
    "luko", "luko-insurance", "alan-insurance", "qover", "qover-com",
    "lockton", "lockton-jobs", "willis-towers", "willistowerswatson", "wtw",
    "marsh", "marshmclennan", "marsh-mclennan", "aon", "aon-jobs", "gallagher",
    "ajg", "arthur-j-gallagher", "brown-and-brown", "brownandbrown",
    "hub-international", "hubinternational", "alliant", "alliant-insurance",
    "usi", "usi-insurance", "epic-insurance", "epicinsurance",

    # ── Agritech / Food-tech ───────────────────────────────────────────────
    "indigo-ag", "indigoag", "farmers-business-network", "fbn",
    "the-climate-corporation", "climate-corp", "granular", "granularag",
    "iteris", "iteris-com", "agworld", "ag-world", "agleader", "ag-leader",
    "trimble-ag", "blue-river", "blueriver", "blue-river-tech", "ouster",
    "ouster-com", "smart-ag", "smartag", "carbon-robotics", "carbonrobotics",
    "agrobot", "agrobotcom", "abundant-robotics", "abundantrobotics",
    "small-robot", "smallrobot", "small-robot-company", "monarch-tractor",
    "monarchtractor", "john-deere", "johndeere", "deere", "deere-jobs",
    "agco", "agco-jobs", "kubota", "case-ih", "caseih", "new-holland",
    "newholland", "claas", "fendt", "massey-ferguson", "masseyferguson",
    "valmont", "valmont-industries", "rivulis", "netafim", "lindsay",
    "lindsay-corp", "cropx", "crop-x", "ceres-imaging", "ceresimaging",
    "taranis", "taranis-ag", "raven-industries", "ravenindustries",
    "trimble-agriculture", "indigoag-jobs", "indigo-jobs", "pivot-bio",
    "pivotbio", "fyto", "fytostock", "growing-underground", "growingunderground",
    "infarm", "in-farm", "fifth-season", "fifthseason", "plenty", "plenty-ag",
    "bowery", "bowery-farming", "boweryfarming", "aerofarms", "aero-farms",
    "appharvest", "app-harvest", "kalera", "babylon-microfarms",
    "babylonmicrofarms", "iron-ox", "ironox", "fifthseasonfresh",
    "vertical-harvest", "verticalharvest", "bayer", "bayer-crop", "syngenta",
    "corteva", "basf", "basf-jobs", "fmc", "fmc-corporation", "nutrien",
    "yara", "yara-international", "icl", "icl-group", "k-and-s", "kandsag",
    "mosaic", "mosaic-co",

    # ── Pet care / Pet tech ────────────────────────────────────────────────
    "chewy", "chewy-jobs", "petco", "petco-jobs", "petsmart", "petsmart-jobs",
    "petfood-direct", "petfooddirect", "wagwalking", "wag", "wag-walking",
    "rover", "rover-com", "barkbox", "bark", "bark-co", "the-farmer-s-dog",
    "thefarmersdog", "ollie", "ollie-pets", "spot-and-tango", "spotandtango",
    "nom-nom", "nomnom", "pupper", "pupper-pet", "darwins", "darwin-s-pet",
    "freshpet", "fresh-pet", "stella-and-chewy", "stellaandchewy", "open-farm",
    "openfarm", "blue-buffalo", "bluebuffalo", "wellness-pet", "wellnesspet",
    "merrick", "merrick-pet", "natural-balance", "naturalbalance", "iams",
    "purina", "hills-pet", "hillspet", "royal-canin", "royalcanin", "eukanuba",
    "embark-vet", "embarkvet", "wisdom-panel", "wisdompanel", "basepaws",
    "base-paws", "darwin-vet", "vetster", "fuzzy", "fuzzy-pet", "petdesk",
    "pet-desk", "ezyvet", "ezy-vet", "petable", "modern-animal", "modernanimal",
    "small-door", "smalldoor", "bond-vet", "bondvet", "tender-paws",
    "tenderpaws", "veterinary-emergency-group", "veg-vets",
    "trusted-housesitters", "trustedhousesitters", "dogvacay",

    # ── Industrial / Manufacturing tech / 3D printing ──────────────────────
    "fictiv", "fictiv-com", "xometry", "xometry-com", "protolabs", "proto-labs",
    "shapeways-jobs", "stratasys", "3d-systems", "3dsystems", "carbon-3d",
    "carbon3d", "desktop-metal", "desktopmetal", "markforged", "mark-forged",
    "formlabs", "form-labs", "ultimaker", "ultimakerjobs", "prusa",
    "prusa-research", "bambulab", "bambu-lab", "anycubic", "any-cubic",
    "creality", "elegoo", "fast-radius", "fastradius", "ge-additive",
    "geadditive", "velo3d", "velo-3d", "norsk-titanium", "norsktitanium",
    "vulcanforms", "vulcan-forms", "seurat", "seurattech", "azoth",
    "alloy-enterprises", "alloyenterprises", "boreal-additive", "borealadditive",
    "siemens-additive", "additive-industries", "additiveindustries",
    "katana", "katanamrp", "katana-mrp", "fishbowl", "fishbowlinventory",
    "epicor", "infor", "infor-jobs", "iqms", "iqms-com", "plex", "plex-systems",
    "plexsystems", "rootstock", "rootstock-software", "sage-x3", "sagex3",
    "abas-erp", "abas", "global-shop", "globalshop", "augury", "augury-systems",
    "uptake", "uptaketech", "sight-machine", "sightmachine", "tulip", "tulip-co",
    "tulip-interfaces", "instrumental", "instrumental-ai", "drishti",
    "drishti-tech", "bright-machines", "brightmachines",

    # ── Robotics / Automation ──────────────────────────────────────────────
    "1x", "1x-tech", "figure", "figure-ai", "figure-ai-corp", "apptronik",
    "apptronik-jobs", "sanctuary", "sanctuary-ai", "sanctuaryai", "unitree",
    "unitree-robotics", "anybotics", "anybots", "ghost-robotics-jobs",
    "skydio-jobs", "shield-ai-jobs", "anduril-jobs-corp", "saronic-jobs",
    "sea-machines", "seamachines", "saildrone", "sail-drone", "ocean-aero",
    "oceanaero", "neura", "neura-robotics", "neurarobotics", "diligent-robotics",
    "diligentrobotics", "savioke", "saviokelabs", "starship", "starship-tech",
    "starshiptech", "covariant", "covariant-ai", "rapyuta", "rapyutarobotics",
    "veo", "veo-robotics", "veorobotics", "realtime-robotics", "realtimerobotics",
    "righthand", "right-hand-robotics", "rios", "rios-corp", "rios-co",
    "soft-robotics", "softrobotics", "softbank-robotics", "kuka", "fanuc",
    "yaskawa", "yaskawa-electric", "abb-robotics", "abbrobotics",
    "kawasaki-robotics", "kawasakirobotics", "epson-robots", "epsonrobots",
    "universal-robots", "universalrobots", "doosan-robotics", "doosanrobotics",

    # ── VR / AR / Metaverse / Quantum ──────────────────────────────────────
    "meta-reality-labs", "meta-rl", "oculus", "horizon-workrooms",
    "horizonworkrooms", "magic-leap", "magicleap", "varjo", "varjo-jobs",
    "vrgineers", "vr-gineers", "xtal-vr", "valve-index", "pico-vr",
    "pico-interactive", "pico-tech", "htc-vive", "htcvive", "snap-spectacles",
    "spectacles", "snap-ar", "niantic", "niantic-labs", "ubiquity6",
    "ubiquity-6", "8th-wall", "8thwall", "blippar", "wikitude", "vuforia",
    "ziva-dynamics", "zivadynamics", "didimo", "didi-mo", "rendered-ai",
    "renderedai", "loom-ai", "loomai", "live-link-face", "livelinkface",
    "wonder-dynamics", "wonderdynamics", "anything-world", "anythingworld",
    "ibm-quantum", "google-quantum", "googlequantum", "rigetti",
    "rigetti-computing", "rigetticomputing", "ionq", "ion-q", "psiquantum",
    "psi-quantum", "atom-computing", "atomcomputing", "quera", "quera-computing",
    "qera", "qed-c", "infleqtion", "infleqtion-jobs", "alice-bob", "aliceandbob",
    "alice-and-bob", "pasqal", "pasqal-com", "quandela", "quandela-jobs",
    "iqm", "iqm-quantum", "iqm-finland", "oxford-quantum-circuits", "oqc",
    "qc-ware", "qcware", "zapata", "zapata-ai", "zapataai", "classiq",
    "classiq-tech", "cqc", "cambridge-quantum", "quantinuum", "qedma",
    "qedma-quantum", "multiverse-computing", "multiverse-quant",
    "horizon-quantum", "horizonquantum", "qunova", "qunovacomputing",
    "tomsk-quantum", "quantum-x", "qx", "quantum-machines", "qm",

    # ── YC W19–S25 (recent batches, expanded coverage) ──────────────────────
    "anyscale-yc", "modal-yc", "rappi-yc", "ironclad-yc", "vercel-yc",
    "fly-yc", "supabase-yc", "retool-yc", "checkr-yc", "amplitude-yc",
    "stripe-yc", "airbnb-yc", "instacart-yc", "doordash-yc", "reddit-yc",
    "twitch-yc", "dropbox-yc", "coinbase-yc",
    "spinwheel", "spin-wheel", "petal", "petal-card", "tomo", "tomo-credit",
    "esusu", "esusu-financial", "self-financial", "selffinancial", "self-lender",
    "credit-genie", "creditgenie", "credit-strong", "creditstrong", "extra",
    "extra-card", "fizz", "fizz-money", "till", "till-financial", "current-bank",
    "yotta", "yotta-savings", "yotta-bank", "ando", "ando-money",
    "ellevest-yc", "rocket-money", "rocketmoney", "truebill", "true-bill",
    "ramp-network", "fairmint", "republic-crypto",
    "vercel-com-yc", "linear-app-yc", "cron-com", "cron", "raycast-com",
    "raycast-jobs", "warpdotdev", "fig-io", "tabby-com", "wezterm",
    "fly-io-jobs", "tigris-data", "tigris", "convex", "convex-dev", "convex-cloud",
    "neon-tech-jobs", "supabase-jobs", "planetscale-jobs", "fauna-jobs",
    "xata", "xata-io", "edge-db", "edgedb", "questdb", "quest-db", "timescale",
    "timescaledb", "timescale-com", "influxdata", "influxdb", "couchbase-jobs",
    "rockset-yc", "tinybird-jobs", "starburst-yc", "dremio-yc",
    "incident-io-yc", "rootly-yc", "fire-hydrant-yc", "blameless-yc",
    "spike-sh-yc", "all-quiet", "allquiet", "uptime-com-yc", "checkly-yc",
    "stably", "stably-ai", "preflight", "preflight-com", "rainforest-qa",
    "rainforestqa", "applitools", "browserstack", "browser-stack", "saucelabs",
    "sauce-labs", "lambdatest", "lambda-test", "perfecto", "perfecto-mobile",
    "kobiton", "headspin", "head-spin", "experitest", "tricentis", "katalon",
    "katalon-com", "playwright", "selenium", "cypress", "cypress-io",
    "testim", "testim-io", "mabl", "mabl-com", "functionize", "function-ize",
    "leapwork", "leap-work", "qase", "qase-io", "testrail", "test-rail",
    "qase-team", "testpad", "testpad-io",
    "scribe-yc", "guidde-yc", "tango-yc", "userlane-yc", "whatfix-yc",
    "walkme-yc", "appcues-yc", "spekit-yc", "pendo-yc", "userpilot-yc",
    "intercom-yc", "drift-yc", "qualified-yc", "chili-piper-yc", "calendly-yc",

    # ── Misc additional well-known startups (long tail) ─────────────────────
    "zapier", "n8n", "make", "make-com", "tray-io", "workato", "automate-io",
    "boomi", "snaplogic", "mulesoft", "talend", "informatica-cloud", "alteryx",
    "knime", "dataiku", "datarobot", "h2o-ai", "domino-data-lab",
    "asana", "monday", "clickup", "linear", "height", "shortcut", "trello",
    "jira", "confluence", "notion", "coda", "almanac", "remnote", "obsidian",
    "logseq", "roam-research", "tana", "anytype",
    "crunchbase", "owler", "zoom-info", "rocketreach", "lusha", "datafied",
    "leadgenius", "leadiq", "uplead", "snov", "mailshake", "klenty",
    "saleshandy", "saleshood", "uberflip", "highspot", "showpad", "seismic",
    "mindtickle", "lessonly",
    "hopin", "bizzabo", "cvent", "swapcard", "brella", "splash-that",
    "eventbrite", "ticket-tailor", "tickettailor", "tito", "bevy", "luma",
    "lu-ma", "partiful",
    "gusto-com", "rippling-com", "deel-com", "remote-careers", "oyster-careers",
    "papayaglobal", "velocity-careers", "globalization-careers", "atlas-hxm",
    "remotebase", "remote-base", "letsdeel",
    "netflix-jobs", "spotify-careers", "uber-careers", "airbnb-careers",
    "doordash-careers", "instacart-careers", "lyft-careers", "stripe-careers",
    "square-careers", "block-careers", "coinbase-careers", "robinhood-careers",
    "plaid-careers", "brex-careers", "ramp-careers", "mercury-careers",
    "carta-careers", "deel-careers", "rippling-careers", "lattice-careers",
    "notion-careers", "figma-careers", "discord-careers", "slack-careers",
    "atlassian-careers", "linear-careers", "asana-careers", "monday-careers",
    "clickup-careers", "github-careers", "gitlab-careers", "vercel-careers",
    "netlify-careers", "supabase-careers", "planetscale-careers",
    "neon-careers", "fauna-careers", "convex-careers", "tigris-careers",
    "render-careers", "fly-careers", "railway-careers", "porter-careers",
    "qovery-careers", "northflank-careers",
    "snowflake-careers", "databricks-careers", "fivetran-careers",
    "airbyte-careers", "hightouch-careers", "census-careers",
    "rudderstack-careers", "segment-careers", "amplitude-careers",
    "mixpanel-careers", "heap-careers", "fullstory-careers",
    "datadog-careers", "newrelic-careers", "splunk-careers", "grafana-careers",
    "honeycomb-careers", "lightstep-careers", "chronosphere-careers",
    "axiom-careers", "betterstack-careers",
    "okta-careers", "auth0-careers", "ping-careers", "stytch-careers",
    "frontegg-careers", "supertokens-careers", "ory-careers",
    "pagerduty-careers", "incident-careers", "rootly-careers",
    "firehydrant-careers", "blameless-careers",
    "snyk-careers", "checkmarx-careers", "veracode-careers", "sonarsource-careers",
    "stackhawk-careers", "wiz-careers", "panther-careers", "lacework-careers",
    "orca-careers", "abnormal-careers", "knowbe4-careers",
    "crowdstrike-careers", "sentinelone-careers", "tanium-careers",
    "netskope-careers", "zscaler-careers", "darktrace-careers", "vectra-careers",
    "extrahop-careers",
    "anduril-careers", "skydio-careers", "shield-ai-careers", "saronic-careers",
    "epirus-careers", "spacex-careers", "blue-origin-careers",
    "rocket-lab-careers", "relativity-careers", "varda-careers",
    "axiom-space-careers", "voyager-space-careers", "sierra-space-careers",
    "redwire-careers", "iridium-careers", "viasat-careers",
    "openai-careers", "anthropic-careers", "cohere-careers", "mistral-careers",
    "perplexity-careers", "scale-careers", "huggingface-careers",
    "stability-ai-careers", "runway-careers", "elevenlabs-careers",
    "character-ai-careers", "inflection-careers", "adept-careers",
    "imbue-careers", "pika-careers", "together-careers", "replicate-careers",
    "fireworks-careers", "anyscale-careers", "modal-careers",
    "lambda-labs-careers", "coreweave-careers", "groq-careers",
    "cerebras-careers", "weights-and-biases-careers", "comet-ml-careers",
    "neptune-ai-careers", "arize-careers", "fiddler-careers", "aporia-careers",
    "whylabs-careers", "tecton-careers", "feast-careers", "hopsworks-careers",
    "dbt-labs-careers", "elementary-careers", "monte-carlo-careers",
    "great-expectations-careers", "soda-careers", "alation-careers",
    "atlan-careers", "glean-careers", "guru-careers", "mem-ai-careers",
    "writer-careers", "jasper-careers", "synthesia-careers", "heygen-careers",
    "tavus-careers", "descript-careers", "krisp-careers", "otter-careers",
    "fireflies-careers", "deepgram-careers", "assemblyai-careers",
    "speechmatics-careers", "harvey-careers", "spellbook-careers",
    "evisort-careers", "ironclad-careers", "magic-careers", "factory-careers",
    "cursor-careers", "codeium-careers", "tabnine-careers",
    "sourcegraph-careers", "supermaven-careers", "augment-careers",
    "continue-careers", "windsurf-careers", "cognition-careers",
    "lindy-careers", "decagon-careers", "sierra-careers", "rasa-careers",
    "snorkel-careers", "labelbox-careers", "humanloop-careers",
    "promptlayer-careers", "lakera-careers", "voyage-careers", "nomic-careers",
    "browserbase-careers", "deepgram-careers", "predibase-careers",
    "outerbounds-careers", "encord-careers", "pachyderm-careers",
    "iterative-careers", "modular-careers", "mosaicml-careers", "octoml-careers",
    "deci-careers",
    "shopify-careers", "bigcommerce-careers", "klaviyo-careers",
    "attentive-careers", "postscript-careers", "yotpo-careers", "okendo-careers",
    "rebuy-careers", "smartrr-careers", "recharge-careers", "loop-careers",
    "happy-returns-careers", "narvar-careers", "aftership-careers",
    "shipbob-careers", "shipstation-careers", "easypost-careers",
    "shippo-careers", "gorgias-careers", "richpanel-careers", "klar-careers",
    "ortto-careers",
    "hims-careers", "ro-careers", "noom-careers", "calibrate-careers",
    "everlywell-careers", "wisp-careers", "lemonaid-careers", "nurx-careers",
    "lola-careers", "thirty-madison-careers", "keeps-careers", "musely-careers",
    "rory-careers", "maven-careers", "tia-careers", "kindbody-careers",
    "carrot-careers", "progyny-careers", "alto-careers", "capsule-careers",
    "blink-health-careers", "goodrx-careers", "labcorp-careers",
    "thorne-careers", "function-health-careers", "insidetracker-careers",
    "levels-careers", "lumen-careers", "tonal-careers", "peloton-careers",
    "echelon-careers", "tempo-careers", "mirror-careers", "future-careers",
    "freeletics-careers", "fitbod-careers", "headspace-careers",
    "calm-careers", "happify-careers", "youper-careers", "wysa-careers",
    "woebot-careers", "talkspace-careers", "betterhelp-careers", "ginger-careers",
    "modern-health-careers", "lyra-careers", "spring-health-careers",
    "brightline-careers", "headway-careers", "alma-careers", "octave-careers",
    "two-chairs-careers", "rula-careers",
    "veeva-careers", "medidata-careers", "iqvia-careers", "syneos-careers",
    "icon-careers", "parexel-careers", "pra-careers", "covance-careers",
    "ppd-careers", "premier-research-careers", "medpace-careers",
    "epic-careers", "cerner-careers", "athenahealth-careers",
    "allscripts-careers", "veradigm-careers", "nextgen-careers",
    "drchrono-careers", "kareo-careers", "tebra-careers", "elation-careers",
    "carbon-health-careers", "one-medical-careers", "forward-careers",
    "parsley-health-careers", "galileo-careers", "98point6-careers",
    "k-health-careers", "ada-careers", "babylon-careers", "doctolib-careers",
    "teladoc-careers", "amwell-careers", "mdlive-careers",
    "doctor-on-demand-careers", "plushcare-careers", "sesame-careers",
    "flatiron-careers", "tempus-careers", "syapse-careers",
    "foundation-medicine-careers", "guardant-careers", "veracyte-careers",
    "exact-sciences-careers", "natera-careers", "myriad-careers",
    "invitae-careers", "color-careers", "ancestry-careers", "23andme-careers",
    "helix-careers", "10x-genomics-careers", "pacbio-careers",
    "oxford-nanopore-careers", "illumina-careers", "biontech-careers",
    "moderna-careers", "novavax-careers", "regeneron-careers", "biogen-careers",
    "vertex-careers", "alnylam-careers", "bluebird-careers", "beam-careers",
    "prime-medicine-careers", "arc-careers", "crispr-careers",
    "intellia-careers", "editas-careers", "recursion-careers",
    "insilico-careers", "schrodinger-careers", "atomwise-careers",
    "absci-careers", "exscientia-careers", "benevolentai-careers",
    "isomorphic-labs-careers", "iambic-careers", "generate-bio-careers",
    "benchling-careers", "synthego-careers", "twist-careers", "ginkgo-careers",
    "zymergen-careers",
    "watershed-careers", "patch-careers", "pachama-careers", "sylvera-careers",
    "isometric-careers", "carbon-direct-careers", "wren-careers",
    "ecologi-careers", "octopus-energy-careers", "tesla-energy-careers",
    "sunrun-careers", "sunpower-careers", "sunnova-careers", "enphase-careers",
    "solaredge-careers", "first-solar-careers", "aurora-solar-careers",
    "energysage-careers", "palmetto-careers", "freedom-solar-careers",
    "goodleap-careers", "form-energy-careers", "ess-tech-careers",
    "ambri-careers", "natron-careers", "moxion-careers",
    "redwood-materials-careers", "ascend-elements-careers", "li-cycle-careers",
    "northvolt-careers", "freyr-careers", "italvolt-careers", "verkor-careers",
    "blink-charging-careers", "evgo-careers", "chargepoint-careers",
    "wallbox-careers", "ionity-careers", "electrify-america-careers",
    "stem-careers", "fluence-careers",
    "climeworks-careers", "carbfix-careers", "sustaera-careers",
    "verdox-careers", "heirloom-careers", "noya-careers", "ebb-careers",
    "captura-careers", "vesta-careers", "running-tide-careers",
    "planetary-careers", "twelve-careers", "lanzatech-careers",
    "solidia-careers", "carbicrete-careers", "carboncure-careers",
    "fortera-careers",
    "kobold-careers", "earth-ai-careers", "kettle-careers", "demex-careers",
    "raincoat-careers", "tomorrow-io-careers", "atmo-careers",
    "salient-careers", "windward-careers", "spire-careers", "planet-careers",
    "iceye-careers", "capella-careers", "umbra-careers", "albedo-careers",
    "muon-space-careers",
]


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run the same command — checkpoint will resume "
              "from where it stopped.", flush=True)
        sys.exit(0)
    except Exception as e:
        print(f"\nFATAL: {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)

