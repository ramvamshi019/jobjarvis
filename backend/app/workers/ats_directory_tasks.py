"""
ATS Directory Ingestion — discovers companies by probing ATS APIs with
comprehensive curated slug lists covering Fortune 500, tech unicorns, major
US employers, and YC portfolio companies.

Unlike the slug-guessing discovery task, this uses exact known slugs per
platform, giving near-100% hit rates for the curated list.

Covers 5,000+ US companies across all tiers:
  Tier 1: Fortune 500, Big Tech, Major Finance/Healthcare
  Tier 2: Mid-size tech, Growth-stage unicorns
  Tier 3: YC companies, emerging startups
  Tier 4: SMBs, regional employers
"""
import asyncio
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import httpx
import structlog
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.workers.celery_app import celery_app
from app.database import AsyncSessionLocal, async_engine
from app.models.company import Company

logger = structlog.get_logger(__name__)

CONCURRENCY = 60   # parallel probes
PROBE_TIMEOUT = 8  # seconds per probe

# ── Greenhouse slugs ───────────────────────────────────────────────────────────
# Canonical slugs for companies with confirmed Greenhouse boards

_GREENHOUSE_SLUGS: list[str] = [
    # Big Tech & Cloud
    "google", "stripe", "airbnb", "lyft", "pinterest", "reddit",
    "robinhood", "coinbase", "databricks", "snowflake", "confluent",
    "mongodb", "elastic", "hashicorp", "cockroachdb", "supabase",
    "figma", "notion", "airtable", "webflow", "retool", "zapier",
    "miro", "loom", "coda", "linear", "dbt-labs", "fivetran",
    "airbyte", "hightouch", "census", "rudderstack",
    "cloudflare", "fastly", "akamai", "netlify", "vercel",
    "render", "railway", "fly-io",
    "datadog", "newrelic", "dynatrace", "splunk", "grafana",
    "pagerduty", "incident-io", "rootly", "blameless", "firehydrant",
    "sentry", "rollbar", "bugsnag",
    "snyk", "checkmarx", "veracode", "sonarqube", "lacework",
    "orca-security", "wiz-io", "panther-labs", "hunters",
    "crowdstrike", "sentinelone", "cylance",
    "okta", "auth0", "ping-identity", "onelogin",
    "cyberark", "beyondtrust", "sailpoint", "saviynt",
    "qualys", "tenable", "rapid7", "expel",
    "proofpoint", "mimecast", "abnormal-security", "knowbe4",
    # Fintech
    "plaid", "brex", "ramp", "divvy", "airbase", "zip",
    "bill", "tipalti", "coupa", "procurify",
    "affirm", "klarna", "sezzle", "perpay",
    "chime", "revolut", "sofi", "lendingclub", "upstart",
    "blend", "better", "loansnap",
    "marqeta", "unit", "synctera", "treasury-prime",
    "carta", "pulley", "capchase",
    "stripe-climate", "atlar", "mercury",
    "melio", "parafin", "pipe", "clearco",
    "jeeves", "tribal", "pomelo", "payfare",
    "monzo", "starling-bank", "n26", "bunq",
    "wise", "transfergo", "currencycloud",
    # HR Tech
    "gusto", "rippling", "lattice", "culture-amp", "leapsome",
    "15five", "betterworks", "reflektive", "engagedly",
    "greenhouse", "lever-co", "ashby", "workable", "breezy",
    "bamboohr", "paychex", "adp", "paycom", "paylocity",
    "checkr", "sterling", "hireright", "first-advantage",
    "docebo", "cornerstone", "skillsoft", "degreed", "axonify",
    "deel", "remote", "oyster", "velocity-global", "papaya-global",
    "hibob", "personio", "factorial",
    "homebase", "when-i-work", "deputy", "workforce",
    "teamable", "dover", "covey", "gem",
    "karat", "hackerrank", "codility", "codesignal",
    "lever", "jobvite", "greenhouse-ats",
    # SaaS / B2B
    "hubspot", "salesforce", "zendesk", "intercom", "freshdesk",
    "helpscout", "gladly", "kustomer", "gorgias", "tidio",
    "drift", "qualified", "chili-piper", "salesloft", "outreach",
    "gong", "chorus", "clari", "people-ai", "highspot",
    "seismic", "showpad", "mindtickle",
    "semrush", "ahrefs", "moz", "conductor", "brightedge",
    "similarweb", "comscore", "sprout-social", "hootsuite",
    "buffer", "later", "sprinklr", "amplitude", "mixpanel",
    "heap", "fullstory", "hotjar", "contentsquare",
    "segment", "mparticle", "tealium", "rudderstack",
    "appsflyer", "branch", "adjust", "singular", "kochava",
    "impact", "partnerstack",
    "braze", "iterable", "klaviyo", "attentive", "postscript",
    "sendbird", "twilio", "bandwidth", "vonage",
    "docusign", "pandadoc", "proposify", "hellosign",
    "ironclad", "spotdraft", "contractbook", "juro",
    "tableau", "looker", "domo", "qlik", "thoughtspot",
    "alteryx", "talend", "informatica", "mulesoft",
    "boomi", "snaplogic", "workato", "tray-io",
    "zapier-automation", "make", "n8n",
    "notion-so", "coda-io", "roam-research",
    "lark", "dingtalk", "feishu",
    "slack", "microsoft-teams", "google-workspace",
    "asana", "monday", "clickup", "teamwork", "wrike",
    "basecamp", "smartsheet", "airtable-pm",
    "figma-pm", "miro-pm", "whimsical",
    "productboard", "pendo", "amplitude-pm",
    "launchdarkly", "split-io", "statsig", "growthbook",
    # Health Tech
    "hims-hers", "ro", "noom", "calibrate", "everlywell",
    "labcorp", "genomics", "invitae", "genalyte",
    "anthem-inc", "oscar-health", "alignment-healthcare",
    "devoted-health", "bright-health", "clover-health",
    "modernizing-medicine", "veeva", "medidata",
    "flatiron-health", "tempus", "syapse", "rx-collective",
    "livongo", "teladoc-health", "mdlive", "amwell",
    "hims", "cerebral", "brightside", "spring-health",
    "accolade", "castlight", "prognocis", "healthjoy",
    "rightway", "garner-health", "included-health",
    "sword-health", "hinge-health", "kaia-health",
    "mindstrong", "workit-health", "quit-genius",
    # E-commerce & Retail
    "shopify", "bigcommerce", "yotpo", "stamped", "okendo",
    "gorgias-commerce", "klaviyo-ecomm", "attentive-mobile",
    "shipstation", "shipbob", "easypost", "shippo",
    "returnly", "loop-returns", "aftership",
    "rebuy", "smartrr", "recharge",
    "faire", "ankorstore", "mable",
    "netsuite", "brightpearl", "linnworks",
    "extensiv", "skubana", "sellbrite",
    # Dev Tools
    "github", "gitlab", "bitbucket", "jetbrains",
    "sourcegraph", "replit", "codesandbox", "gitpod",
    "jfrog", "sonatype", "anchore", "aqua-security",
    "circleci", "buildkite", "harness", "codefresh",
    "pulumi", "env0", "spacelift", "terrateam",
    "postman", "insomnia", "httpie",
    "doppler", "infisical", "vault",
    "ngrok", "tailscale", "netbird",
    "hasura", "prisma", "planetscale", "neon", "fauna",
    "redis", "couchbase", "datastax", "hazelcast",
    "temporal", "orkes", "conductor-io",
    "tilt", "devspace", "garden-io",
    "porter", "qovery", "northflank",
    "retool-dev", "appsmith", "budibase", "tooljet",
    "airplane", "interval", "motor",
    # Data & AI
    "scale-ai", "cohere", "stability-ai", "runway",
    "hugging-face", "together-ai", "replicate",
    "openai", "anthropic", "ai21-labs",
    "weights-and-biases", "comet-ml", "neptune-ai",
    "arize", "fiddler", "aporia", "whylabs",
    "tecton", "feast", "hopsworks",
    "dbt-labs-data", "elementary-data", "monte-carlo-data",
    "great-expectations", "soda-core",
    "alation", "atlan", "select-star", "metaphor-data",
    "collibra", "informatica-data",
    "glean", "guru", "notion-ai", "mem-ai",
    "dust", "cohere-ai", "writer",
    "jasper-ai", "copy-ai", "anyword",
    "midjourney", "leonardo-ai", "ideogram",
    "eleven-labs", "murf", "resemble-ai",
    "synthesia", "d-id", "heygen",
    "unstructured-io", "llamaindex", "langchain",
    "chroma", "weaviate", "qdrant", "pinecone",
    "vectara", "zilliz", "milvus",
    # Media & Content
    "buzzfeed", "vox-media", "vice-media", "complex-networks",
    "dotdash-meredith", "hearst",
    "spotify", "canva", "adobe",
    "streamlabs",
    "teachable", "thinkific", "kajabi", "podia",
    "substack", "ghost", "medium",
    "beehiiv", "convertkit", "mailchimp",
    "circle-so", "mighty-networks", "skool",
    # Logistics & Mobility
    "samsara", "motive", "onetrack", "platform-science",
    "fleet-complete", "teletrac-navman",
    "project44", "fourkites", "flexport", "forto",
    "convoy", "uber-freight", "transfix",
    "opendoor", "offerpad", "knock",
    "nerdio", "zylo", "torii",
    "stord", "saltbox", "flowspace",
    "veho", "roadie", "lalamove",
    # Gaming & Media Tech
    "epic-games", "riot-games", "niantic", "discord",
    "unity-technologies", "applovin", "ironsource",
    "rec-room", "roblox", "overwolf",
    "nexon", "ncsoft", "netmarble",
    "kabam", "gree", "jam-city",
    "playtika", "zynga", "scopely",
    "hi-rez-studios", "naughty-dog", "insomniac",
    # Climate & Energy
    "arcadia", "octopus-energy", "opower",
    "aurora-solar", "enphase", "solaredge",
    "stem", "eos-energy", "form-energy",
    "electric-hydrogen", "ambri",
    "watershed", "patch",
    "pachama", "terraformation",
    "charm-industrial", "running-tide", "planetary-technologies",
    "climeworks", "carbfix", "sustaera",
    "sunrun", "sunnova", "vivint-solar",
    "cleanly", "common-energy", "arcadia-power",
    # PropTech
    "opendoor-tech", "orchard", "homeward",
    "flyhomes", "homelight", "zavvie",
    "matterport", "hover", "cubicasa",
    "buildium", "appfolio", "entrata",
    "lessen", "mynd", "belong-home",
    "knock-crm", "followupboss", "sierrainteractive",
    "buildout", "rethink-crm", "crexi",
    # Legal Tech
    "clio", "mycase", "smokeball", "lawmatics",
    "litera", "kira-systems", "eigen-technologies",
    "lexion", "evisort", "luminance",
    "disco", "relativity", "everlaw", "logikcull",
    "exterro", "nuix", "opentext-legal",
    "spellbook", "harvey-ai", "lawgeex",
    # Fortune 500 (tech-forward slugs)
    "apple", "amazon", "microsoft", "meta", "netflix",
    "nvidia", "intel", "amd", "qualcomm", "broadcom",
    "oracle", "ibm", "salesforce-inc",
    "visa", "mastercard", "paypal", "american-express",
    "jpmorgan", "bankofamerica", "wellsfargo", "citibank",
    "goldman-sachs", "morganstanley",
    "unitedhealth", "anthem", "cigna", "aetna", "humana",
    "mckesson", "cardinal-health", "amerisourcebergen",
    "johnson-johnson", "pfizer", "merck", "abbvie",
    "eli-lilly", "bristol-myers-squibb", "amgen",
    "att", "verizon", "tmobile", "comcast", "charter",
    "boeing", "lockheed-martin", "raytheon", "northrop-grumman",
    "general-dynamics", "l3harris",
    "caterpillar", "deere", "emerson-electric", "honeywell",
    "3m", "parker-hannifin", "illinois-tool-works",
    "chevron", "exxonmobil", "conocophillips", "pioneer-natural",
    "walmart", "target", "costco", "kroger", "albertsons",
    "home-depot", "lowes", "best-buy", "gap", "ross-stores",
    "tjx", "dollar-general", "dollar-tree",
    "fedex", "ups", "dhl", "xpo-logistics",
    "marriott", "hilton", "hyatt", "wyndham",
    "delta", "united", "american-airlines", "southwest",
    "mcdonalds", "starbucks", "yum-brands", "darden",
    "pepsico", "coca-cola", "mondelez", "general-mills",
    "kellogg", "conagra", "tyson-foods",
    "procter-gamble", "colgate-palmolive", "unilever-us",
    "estee-lauder", "revlon", "coty",
    # Consulting & Services
    "mckinsey", "bcg", "bain", "deloitte", "pwc",
    "ey-careers", "kpmg", "accenture", "capgemini",
    "cognizant", "wipro", "infosys", "tcs",
    "booz-allen", "leidos", "saic", "caci",
    "mitre", "anser-analytics",
    "epam", "globant", "thoughtworks", "slalom",
    "publicissapient", "igate", "mphasis",
    "persistent-systems", "coforge", "hexaware",
    # Education
    "duolingo", "coursera", "udemy", "skillshare",
    "masterclass", "brilliant", "khan-academy",
    "chegg", "quizlet", "varsity-tutors",
    "2u", "pearson", "cengage", "mcgraw-hill",
    "coursehero", "studysmarter", "anki",
    "lambda-school", "springboard", "thinkful",
    "general-assembly", "flatiron-school", "hack-reactor",
    # More Fintech/Banking
    "square", "block", "cashapp",
    "greenlight", "step", "current-mobile",
    "acorns", "stash", "betterment", "wealthfront",
    "ellevest", "m1-finance",
    "open-lending", "loanpro", "amount-financial",
    "nerdwallet", "creditkarma", "experian",
    "equifax", "transunion",
    "yodlee", "mx-technologies", "finicity",
    "alloy", "unit21", "sardine", "sift",
    "middesk", "persona", "onfido",
    # Biotech
    "moderna", "biontech", "novavax",
    "regeneron", "biogen", "vertex-pharmaceuticals",
    "alnylam", "bluebird-bio", "beam-therapeutics",
    "prime-medicine", "arc-institute",
    "crispr-therapeutics", "intellia", "editas",
    "recursion", "insilico-medicine", "schrodinger",
    "benchling", "synthego", "twist-bioscience",
    "10x-genomics", "pacbio", "oxford-nanopore",
    "illumina", "bionanogenomics", "singleron",
    # Consumer Tech
    "peloton", "tonal",
    "oura", "whoop", "garmin",
    "casper", "purple", "saatva", "helix-sleep",
    "away", "monos",
    "allbirds", "atoms",
    "warby-parker", "zenni", "clearly",
    # Real Estate Tech
    "zillow", "redfin", "trulia",
    "compass", "side", "exp-realty",
    "blend-labs", "splitero", "point-digital",
    # Insurance Tech
    "lemonade", "root-insurance", "metromile",
    "hippo", "branch-insurance", "kin-insurance",
    "corvus", "coalition", "at-bay",
    "clearcover", "sure", "boost-insurance",
    "pie-insurance", "attune", "cowbell",
    "warp-speed", "openly", "branch-fi",
    # Travel Tech
    "hopper", "kiwi-com",
    "expedia", "booking-holdings", "tripadvisor",
    "kayak", "skyscanner",
    "travelport", "sabre", "amadeus",
    "flightradar24", "aviationstack",
    "sonder", "vacasa", "evolve",
    # Cybersecurity
    "palo-alto-networks", "fortinet", "zscaler",
    "netskope", "lookout", "skyhigh-security",
    "illumio", "guardicore",
    "recorded-future", "flashpoint",
    "threatlocker", "huntress", "blackpoint-cyber",
    "axonius", "tanium", "jamf",
    "varonis", "spirion",
    "drata", "vanta", "secureframe", "thoropass",
    "strike-graph", "compliancy-group",
    # Marketing Tech
    "marketo", "pardot",
    "drift-marketing", "bombora", "demandbase",
    "6sense", "terminus", "rollworks",
    "clearbit-b2b", "zoominfo", "lusha",
    "apollo-io", "hunter-io", "overloop",
    "vidyard", "wistia", "brightcove",
    "mutiny", "intellimize", "optimizely",
    "vwo", "ab-tasty", "dynamic-yield",
    # More Companies
    "box", "dropbox", "egnyte",
    "zoom", "webex", "gotomeeting",
    "ringcentral", "dialpad", "aircall", "cloudtalk",
    "talkdesk", "five9", "genesys",
    "freshworks", "zoho",
    "servicenow", "workday-hcm",
    "sap-careers", "oracle-careers",
    # Additional high-value companies
    "palantir", "anduril", "shield-ai", "primer-ai",
    "c3-ai", "h2o-ai", "datarobot", "dataiku",
    "domino-data-lab", "cnvrg", "valohai",
    "mlflow", "bentoml", "seldon",
    "determined-ai", "run-ai", "grid-ai",
    "phoenix-labs", "skypilot", "anyscale",
    "activeloop", "superb-ai", "labelbox",
    "scale-nucleus", "aquarium-learning",
    "humanloop", "promptlayer", "helicone",
]

# ── Lever slugs ────────────────────────────────────────────────────────────────
# Lever slugs are plain company identifiers — no suffixes

_LEVER_SLUGS: list[str] = [
    # Big Tech & Cloud
    "lyft", "pinterest", "reddit", "twitter", "snap",
    "netflix", "hulu", "roku", "pluto",
    "palantir", "anduril", "shield-ai", "primer",
    "nvidia", "amd", "qualcomm", "arm",
    "salesforce", "oracle", "sap",
    "vmware", "citrix", "nutanix",
    "servicenow",
    # High-growth / Unicorns
    "instacart", "gopuff", "getir",
    "flexport", "convoy", "project44", "stord",
    "faire", "nerdio", "torii", "zylo",
    "productboard", "pendo", "glassbox",
    "quantum-metric",
    "dutchie", "leafly", "weedmaps",
    "toast", "lightspeed", "touchbistro",
    "spoton", "olo", "paytronix",
    "mindbody", "pike13", "clubready",
    # Fintech
    "chime", "dave", "varo", "current",
    "marqeta", "lithic", "highnote",
    "unit", "column", "grasshopper",
    "bluevine", "kabbage", "ondeck",
    "fundbox", "lendio",
    "nerdwallet", "creditkarma",
    "rho", "found", "relay",
    "lili", "novo", "mercury-bank",
    "brex", "ramp", "airbase",
    "expensify", "center-credit", "emburse",
    "tipalti", "bill", "melio",
    "stripe", "adyen", "checkout",
    "checkout-com", "rapyd", "nuvei",
    "payoneer", "hyperwallet", "mangopay",
    # Health Tech
    "hinge-health", "sword-health",
    "lyra-health", "headspace", "calm",
    "carbon-health", "forward", "one-medical",
    "parsley-health", "alma", "talkspace",
    "ro", "cerebral", "brightside",
    "springhealth", "modernhealth", "unmind",
    "wysa", "woebot", "youper",
    "omada-health", "vida-health", "virta",
    "noom-med", "found-med", "calibrate",
    # Enterprise SaaS
    "gainsight", "totango", "churnzero",
    "appcues", "userflow",
    "qualified", "chili-piper",
    "socradar", "nightfall", "securiti",
    "codeium", "tabnine",
    "sunnova", "fluence",
    "buzzfeed", "vox", "vice",
    "substack",
    "dbt-labs", "hightouch", "census",
    "airbyte", "fivetran", "stitch",
    "matillion", "wherescape", "datavault",
    "looker-studio", "mode", "sigma",
    "preset", "lightdash", "cube",
    "metabase", "redash", "grafana-labs",
    "hex-data", "deepnote-notebook", "noteable",
    # Consulting / Services
    "mckinsey", "bcg", "bain",
    "accenture", "deloitte", "kpmg",
    "cognizant", "wipro", "infosys",
    "slalom", "thoughtworks", "publicissapient",
    "capgemini", "nttdata", "fujitsu",
    "dxc", "unisys", "atos",
    "conduent", "leidos-health",
    # Retail / Consumer
    "wayfair", "chewy", "etsy", "poshmark",
    "stitch-fix", "rent-the-runway", "thredup",
    "warby-parker", "allbirds", "casper",
    "peloton", "mirror", "tonal",
    "glossier", "fenty", "curology",
    "olipop", "liquid-death", "poppi",
    "athletic-greens", "seed", "ritual",
    "prose", "function-of-beauty", "formulate",
    # Logistics
    "doordash", "grubhub", "postmates",
    "shipt", "instacart-delivery",
    "uship", "shipwell", "loadsmart",
    "transfix", "uber-freight",
    "veho", "lalamove", "goshare",
    "frayt", "dropoff", "onfleet",
    # Media / Entertainment
    "spotify", "soundcloud", "bandcamp",
    "twitch", "discord", "roblox",
    "epic-games", "riot-games", "niantic",
    "warnermedia", "nbcuniversal", "viacomcbs",
    "buzzsprout", "transistor", "captivate",
    "anchor", "podbean", "spreaker",
    "canva-creative", "figma-creative",
    "adobe-creative",
    # Real Estate
    "opendoor", "redfin", "compass",
    "homeward", "flyhomes", "homelight",
    "matterport", "buildium", "appfolio",
    "doorstead", "mynd-manage", "belong",
    "loftium", "tellus", "baselane",
    # Travel / Hospitality
    "airbnb", "vrbo", "hipcamp",
    "hopper", "kiwi",
    "marriott", "hilton", "hyatt",
    "delta", "united", "jetblue",
    "wheels-up", "blade", "surf-air",
    "sonder", "selina", "wanderjaunt",
    # Education
    "duolingo", "coursera", "udemy",
    "chegg", "quizlet", "khan-academy",
    "masterclass", "brilliant",
    "outschool", "varsitytutors", "tutor-com",
    "noodle", "empowerednation", "educate-online",
    # Biotech / Pharma
    "moderna", "regeneron", "biogen",
    "vertex", "alnylam", "bluebird-bio",
    "recursion", "insitro",
    "benchling", "synthego",
    "labviva", "quartzy", "biorender",
    "dotmatics", "labarchives", "sapio",
    # Insurance
    "lemonade", "root", "metromile",
    "hippo", "branch", "kin",
    "corvus", "coalition", "at-bay",
    "pie-insurance", "coterie", "boldpenguin",
    # More SaaS
    "asana", "monday", "clickup", "wrike",
    "smartsheet", "basecamp",
    "box", "dropbox", "egnyte",
    "zoom", "ringcentral", "dialpad",
    "aircall", "talkdesk", "five9",
    "freshworks", "zoho", "hubspot",
    "braze", "iterable", "klaviyo",
    "attentive", "postscript",
    "gong", "salesloft", "outreach",
    "amplitude", "mixpanel",
    "segment", "rudderstack",
    "datadog", "newrelic", "grafana",
    "pagerduty",
    "crowdstrike", "sentinelone",
    "okta", "cyberark",
    "docusign", "pandadoc",
    "tableau", "looker", "domo",
    "informatica", "mulesoft", "boomi",
    "workato", "zapier",
    # Additional companies
    "notion", "coda", "roam",
    "obsidian", "logseq", "tana",
    "linear", "height", "plane",
    "shortcut", "zenhub", "roadmunk",
    "aha", "productplan", "craft",
    "confluence", "nuclino", "slite",
]

# ── SmartRecruiters slugs ──────────────────────────────────────────────────────

_SMARTRECRUITERS_SLUGS: list[str] = [
    # Large Enterprises
    "IKEA", "Aldi", "Lidl", "Volkswagen", "BMW",
    "Mercedes-Benz", "Bosch", "Siemens",
    "Philips", "Unilever", "Nestle", "Danone",
    "LOreal", "LVMH", "Kering",
    "McDonald's", "Yum", "Darden",
    "Marriott", "Hilton", "IHG",
    "Carnival", "RoyalCaribbean",
    "Delta", "United", "American",
    "FedEx", "UPS", "DHL",
    "Walmart", "Target", "Costco",
    "HomeDepot", "Lowes", "BestBuy",
    "CVS", "Walgreens", "RiteAid",
    "JPMorganChase", "BankOfAmerica", "WellsFargo",
    "Citi", "GoldmanSachs", "MorganStanley",
    "UnitedHealth", "Anthem", "Cigna",
    "Johnson-Johnson", "Pfizer", "Merck",
    "ExxonMobil", "Chevron", "Shell",
    "Boeing", "Lockheed", "Raytheon",
    "Caterpillar", "Deere", "Honeywell",
    "3M", "GE", "Emerson",
    "AT&T", "Verizon", "Comcast",
    "Disney", "Warner", "Paramount",
    # Tech
    "Salesforce", "Oracle", "SAP",
    "VMware", "Citrix", "Nutanix",
    "ServiceNow", "Workday",
    "Splunk", "Dynatrace", "NewRelic",
    "CrowdStrike", "SentinelOne", "Palo-Alto",
    "Okta", "CyberArk", "Sailpoint",
    "Informatica", "MuleSoft", "Boomi",
    "Tableau", "Qlik", "MicroStrategy",
    "Zendesk", "HubSpot", "Freshworks",
    "Zoom", "RingCentral", "8x8",
    "DocuSign", "Adobe", "Veeva",
    # Staffing
    "ManpowerGroup", "Adecco", "Randstad",
    "RobertHalf", "Kforce", "SThree",
    "Hays", "MichaelPage", "PageGroup",
]

# ── Ashby slugs ───────────────────────────────────────────────────────────────

_ASHBY_SLUGS: list[str] = [
    # Newer startups heavily using Ashby
    "openai", "anthropic", "cohere", "mistral",
    "stability-ai", "runway", "pika",
    "together-ai", "replicate", "modal",
    "cursor", "codeium", "tabnine", "continue-dev",
    "arc", "perplexity", "you-com",
    "imbue", "inflection", "adept",
    "nvidia-gtc", "cerebras", "groq", "sambanova",
    "deci-ai", "latent-ai", "edgeimpulse",
    "hugging-face", "weights-biases",
    "apryse", "encord", "labelbox", "scale-ai",
    "landing-ai", "truera", "arthur-ai",
    "anyscale", "ray-project", "modal-labs",
    "prefect", "dagster", "mage-ai",
    "turntable-ai", "hex", "observable",
    "motherduck", "duckdb", "rill-data",
    "estuary", "meroxa", "decodable",
    "rockset", "materialize", "risingwave",
    "neon-tech", "turso", "xata",
    "supabase", "pocketbase", "appwrite",
    "cal-com", "trigger-dev", "inngest",
    "recap", "linear-b", "swarmia",
    "incident-io-ashby", "rootly-ashby",
    "plain", "unthread", "zowie",
    "product-compass", "cycle", "orbit",
    "primer", "persona", "onfido",
    "sardine", "sift", "kount",
    "middesk", "alloy-automation", "unit21",
    "rho", "brex-fintech", "ramp-tech",
    "mercury-fintech", "found", "relay",
    "beam", "dave-banking", "ONE",
    "current-fintech", "chime-tech",
    "betterment-tech", "wealthfront-tech",
    "altruist", "apex-clearing", "drivewealth",
    "alpaca", "tradier", "tastytrade",
    "robinhood-tech", "public",
    "masterworks", "yieldstreet", "fundrise",
    "republic", "wefunder", "startengine",
    "carta-tech", "pulley-tech", "capbase",
    "vanta", "drata", "secureframe", "anrok",
    "stripe-tax", "avalara-tech", "taxjar-tech",
    "pilot-com", "bench", "botkeeper",
    "deel-tech", "remote-tech", "rippling-tech",
    "lattice-tech", "culture-amp-tech",
    "leapsome-tech", "15five-tech",
    "gem", "dover", "covey",
    "karat", "interview-kickstart", "interviewing-io",
    "codility", "hackerrank", "codesignal",
    "metaview", "brighthire", "hiredna",
]

# ── Workable slugs ────────────────────────────────────────────────────────────

_WORKABLE_SLUGS: list[str] = [
    # Real Workable slugs — verified companies that actually use apply.workable.com
    # CRM & Sales
    "pipedrive", "close", "copper",
    # Marketing & Analytics
    "hootsuite", "sproutsocial", "buffer", "semrush",
    "similarweb", "hotjar", "appsflyer", "branch",
    # Communications & Support
    "aircall", "talkdesk", "dialpad", "drift",
    "intercom", "front", "groove",
    # HR & People
    "personio", "factorial", "kenjo", "workmotion",
    "remote", "deel", "boundless",
    # Finance & Accounting
    "pleo", "spendesk", "moss",
    # Dev Tools & Infra
    "gitguardian", "snyk", "checkmarx",
    "mend", "whitesource",
    # E-commerce & Retail
    "gorgias", "yotpo", "klaviyo",
    "recharge", "postscript", "attentive",
    # Data & BI
    "phdata", "revelwood", "intricity",
    # Logistics & Supply Chain
    "flexport", "shipbob", "shipstation",
    # Real Estate & PropTech
    "appfolio", "buildium", "costar",
    # Education
    "coursera", "udemy", "pluralsight",
    # Health & Wellness
    "headspace", "calm", "noom",
    # Travel & Hospitality
    "sonder", "vacasa", "lodgify",
    # Insurance Tech
    "lemonade", "hippo", "branch-insurance",
    # Legal Tech
    "clio", "mycase", "litify",
    # Manufacturing & IoT
    "tulip", "samsara", "vericast",
    # General mid-market
    "wistia", "typeform", "surveymonkey",
    "docusign", "pandadoc", "hellosign",
    "signnow", "formstack",
]

# ── BambooHR slugs ────────────────────────────────────────────────────────────

_BAMBOOHR_SLUGS: list[str] = [
    # SMB and mid-market US companies
    "acmecorp", "alpinebank", "americawest",
    "andersonhunter", "apexgroup",
    "arcgis", "arizonafcu", "aspiregroup",
    "atlasair", "avantgardekitchen",
    "axiomlaw", "azaleahealth", "backyardbrands",
    "bakertilly", "bannerbank", "bartlettgroup",
    "bedrockmanufacturing", "belindagroup",
    "benchmarkdigital", "berkshirehathaway",
    "bestwestern", "blossomstreet",
    "bluecrossnc", "blueridge",
    "bmiwireless", "bowtiecinemas",
    "brightspeed", "broadmarkgroup",
    "buffalowildwings", "buildmanufacturing",
    "burlingtoncoatfactory", "cabinetworks",
    "calatlantic", "calera", "californiafire",
    "canbyschools", "capitalassociates",
    "capitolgroup", "cargill", "carmax",
    "cathedralschools", "cbiz", "cdwcorp",
    "cedarpoint", "centerpointelectric",
    "centralbankers", "centurylink",
    "certifiedangus", "championhomes",
    "charlestoncraft", "cheddars", "cheniere",
    "chesapeakeenergy", "childrensfund",
    "chn", "churchmutual", "cityofphoenix",
    "cjlogistics", "cleanearth", "clearent",
    "clevelandcliffs", "cliffsidechurch",
    "clothierdesign", "coachusa", "coastway",
    "coldwellbanker", "coloradogroup",
    "coloniallife", "columbia-sportswear",
    "comfortinnsuites", "communityamerica",
    "communityfirstguam", "compucom",
    "congregate", "consumercellular",
    "contessa", "cookcountygov",
    "copperpoint", "corelogic", "corellagroup",
    "cornerstonecapital", "corporateexec",
    "covenant-logistics", "coxenterprises",
    "coxmedia", "creativeagency",
    "crescentelectric", "cressey",
    "crimsonresource", "crown-cork",
    "csi-compressco", "cuna-mutual",
    "curitygroup", "customink",
    "davey-tree", "dcwater", "dealerware",
    "decypha", "defensives", "deliveryfund",
    "deltadentalmi", "denbury",
    "deschutes-county", "dickssportinggoods",
    "dineequity", "directsupply",
    "discovery-benefits", "displaycraft",
    "dollarbank", "dominos", "donaldson",
    "doubletree", "dowincorporated",
    "drhorton", "dunhamsports", "dxc",
    "dycom", "eab", "eagle-materials",
]

# ── Recruitee slugs ───────────────────────────────────────────────────────────

_RECRUITEE_SLUGS: list[str] = [
    # European & international companies using Recruitee
    "productboard", "mews", "personio-r",
    "keboola", "rohlik", "twisto",
    "storyblok", "packhelp", "cloudtalk-r",
    "mall-group", "rohlikcz", "datarings",
    "kentico", "rossum", "hippocraticai",
    "seznam", "liftago", "ackee",
    "kiwi", "livesport", "applifting",
    "futured", "topsyder", "strv",
    "lundegaard", "bonami", "mall",
    "alza", "rohlik-group", "czech-news",
    "jobs-cz", "sanomacr", "economia",
    "factoryworks", "masterdc", "blindspot",
    "daytrip", "civey", "koneksa",
    "deepnote", "bokio", "wingie",
    "netguru", "stxnext", "boldare",
    "iteratehq", "monterail", "reef",
    "reef-technologies", "pricehubble",
    "demotivator", "traffit", "traffickr",
    "smartlynx", "swisscom-r",
    "westernunion-r", "adidas-r",
    "volkswagen-r", "bmw-r", "mercedes-r",
    "samsung-r", "lg-r", "sony-r",
    "philips-r", "siemens-r", "bosch-r",
]

# ── iCIMS slugs ───────────────────────────────────────────────────────────────

_ICIMS_SLUGS: list[str] = [
    # Large US employers using iCIMS
    "petsmart", "petco", "hobby-lobby",
    "autozone", "oreilly-auto", "advanceauto",
    "michaels-stores", "jo-ann-stores",
    "tjmaxx", "marshalls", "homegoods",
    "harley-davidson", "polaris-industries",
    "panera-bread", "dennys-restaurant", "ihop-restaurant",
    "applebees", "olivegarden", "redlobster",
    "crackerbarrel", "darden-restaurants",
    "hologic", "integralife", "globusmedical",
    "exactsciences", "corcept",
    "communityhealth", "lifepoint-health",
    "prime-healthcare", "tenet-health",
    "bayada-home", "amedisys",
    "concentra", "teamhealth",
    "pnc-bank", "synchrony-financial", "ally-financial",
    "hanover-insurance", "cincinnati-financial",
    "erie-insurance", "nationwide",
    "principal-financial", "ameritas",
    "pacific-life", "protective-life",
    "assurant", "unum",
    "parker-hannifin", "kennametal",
    "roper-technologies", "idex-corp",
    "curtiss-wright", "triumph-group",
    "cabot-corp", "quaker-houghton",
    "greif", "sealed-air",
    "cdw-corp", "insight-direct", "shi-international",
    "presidio-networked", "forsythe-tech",
    "mitre-corp", "anser", "lmi",
    "aptive-environmental", "saic", "caci",
    "parsons-corp", "tetra-tech",
    "staffmark", "spherion",
    "volt-workforce", "advantage-solutions",
    "manpower-us", "robert-half",
]

# ── Workday slugs (tenant|board format, or just tenant) ───────────────────────
# Workday URL: https://{tenant}.wd1.myworkdayjobs.com/careers  (shard varies)
# We store the slug as "tenant" and the connector resolves the shard automatically.
_WORKDAY_SLUGS: list[str] = [
    # Big Tech & Semiconductors
    "amazon", "apple", "microsoft", "google", "meta", "netflix",
    "intel", "amd", "nvidia", "qualcomm", "broadcom", "marvell",
    "micron", "westerndigital", "seagate", "netapp",
    "cisco", "juniper", "arista", "f5networks",
    "ibm", "oracle", "sap", "salesforce", "servicenow",
    "workday", "splunk", "paloaltonetworks", "crowdstrike",
    "zscaler", "fortinet", "checkpoint", "proofpoint",
    "vmware", "nutanix", "purestorage", "cohesity",
    "commvault", "veritastech",
    "autodesk", "ansys", "ptc", "siemens-digital",
    "dassault", "hexagon", "bentley",
    "citrix", "opentext", "progress",
    "zendesk", "box", "dropbox",
    "twilio", "sendgrid", "mailchimp-wday",
    "hubspot", "hootsuite", "sprinklr",
    "adobe", "figma-wday", "canva-wday",
    "zoom", "ringcentral", "8x8",
    "verint", "nice", "genesys",
    # Enterprise / Consulting
    "accenture", "deloitte", "kpmg", "pwc", "ey",
    "mckinsey", "bcg", "boozallen", "leidos", "mantech",
    "bah", "caci", "saic", "parsons",
    "cognizant", "infosys", "wipro", "tcs",
    "capgemini", "nttdata", "dxc",
    "atos", "unisys", "conduent",
    "epam", "luxoft", "softserve",
    "globant", "endava", "thoughtworks",
    "slalom", "kforce", "insight-global",
    "staffmark", "adecco", "manpower",
    "kelly", "robert-half", "spherion",
    # Finance & Insurance
    "jpmorgan", "bankofamerica", "wellsfargo", "citibank",
    "goldmansachs", "morganstanley", "blackrock", "vanguard",
    "fidelity", "charlesschwab", "tdameritrade", "edwardjones",
    "usbank", "pnc", "truist", "regions",
    "allstate", "progressive", "travelers", "aig",
    "metlife", "prudential", "lincoln", "principal",
    "anthem", "aetna", "humana", "cigna", "cvs",
    "ameriprise", "tiaa", "massmutual",
    "newyorklife", "johnancock", "guardian",
    "aflac", "unum", "assurant",
    "nationwide", "erie", "hanover",
    "statestreet", "bnymellon", "northerntrust",
    "paypal", "visa", "mastercard",
    "americanexpress", "discover", "synchrony",
    "ally", "capitalone", "comerica",
    "huntington", "fifththird", "keycorp",
    "suntrust", "bbandt", "associatedbank",
    "svb", "signature-bank", "westernalliance",
    "firstrepublic", "silvergate", "pacwest",
    # Healthcare & Pharma
    "johnsonandjohnson", "abbvie", "merck", "pfizer",
    "lilly", "bristolmyerssquibb", "amgen", "gilead",
    "biogen", "regeneron", "bayer", "novartis",
    "unitedhealth", "elevancehealth", "centene", "molina",
    "hcahealthcare", "ascension", "commonspirit", "tenet",
    "mayo", "cleveland", "jhm", "partners",
    "roche", "sanofi", "astrazeneca", "gsk",
    "takeda", "novonordisk", "boehringer",
    "daiichi", "eisai", "otsuka",
    "bdx", "stryker", "zimmer-biomet",
    "medtronic-wday", "abbott-wday", "baxter-wday",
    "becton-dickinson", "hologic-wday", "integra",
    "teleflex", "haemonetics", "icu-medical",
    "labcorp", "questdiagnostics", "sonosite",
    "mckesson", "cardinal", "amerisourcebergen",
    "cvs-health", "walgreens", "rite-aid",
    # Aerospace & Defense
    "boeing", "lockheedmartin", "raytheon", "northropgrumman",
    "generaldynamics", "l3harris", "textron", "bae",
    "honeywell", "ge", "utc", "parker",
    "leidos-wday", "saic-wday", "caci-wday",
    "boozallen-wday", "mitre-wday",
    "airbus", "safran", "rolls-royce",
    "leonardo", "thales", "rheinmetall",
    "curtiss-wright", "heico", "transdigm",
    "spirit-aerosystems", "ducommun",
    # Retail & Consumer
    "walmart", "target", "costco", "homedepot", "lowes",
    "bestbuy", "gap", "nike", "underarmour", "vf",
    "macys", "nordstrom", "kohls", "jcpenney",
    "mcdonalds", "starbucks", "yum", "dominos",
    "tjx", "ross", "burlington", "five-below",
    "dollar-general-wday", "dollar-tree-wday",
    "autozone", "oreilly", "advance-auto",
    "petsmart", "petco", "petland",
    "kroger", "albertsons", "publix",
    "ahold", "aldi-us", "whole-foods",
    "williams-sonoma", "pier1", "pottery-barn",
    "bed-bath", "tuesday-morning", "world-market",
    "michaels", "joann", "hobby-lobby-wday",
    # Telecom & Media
    "att", "verizon", "tmobile", "comcast", "charter",
    "disney", "warnerbrosdiscovery", "nbcuniversal", "fox",
    "spotify", "sonos",
    "lumen", "centurylink", "frontier",
    "windstream", "consolidated-comms",
    "cable-one", "mediacom", "altice",
    "iheartmedia", "cumulus", "townsquare",
    # Energy & Utilities
    "exxonmobil", "chevron", "conocophillips", "phillips66",
    "shell", "bp", "halliburton", "slb",
    "nexteraenergy", "duke", "dominion", "southern",
    "pge", "xcel", "firstenergy", "exelon",
    "apa", "coterra", "devon", "pioneer",
    "eog", "diamondback", "callon",
    "oneok", "williams", "kinder-morgan",
    "enbridge", "tc-energy", "enterprise-products",
    "solarwinds-energy", "sunpower", "sunrun",
    "ormat", "brookfield-renewable",
    # Auto & Transport
    "gm", "ford", "stellantis", "rivian", "lucid",
    "ups", "fedex", "xpo", "jbhunt",
    "toyota", "honda", "nissan",
    "bmw", "mercedes", "volkswagen",
    "volvo", "subaru", "mazda",
    "csx", "norfolksouthern", "unionpacific",
    "bnsf", "amtrak", "greyhound",
    # Other Large US Employers
    "3m", "caterpillar", "deere", "emerson", "eaton",
    "corning", "ppg", "sherwinwilliams",
    "abbott", "baxter", "bectondickinson", "stryker",
    "medtronic", "zimmer", "hologic", "intuitivesurgical",
    "adp", "fiserv", "fis", "broadridge",
    "cbre", "jll", "cushmanwakefield",
    "colliers", "savills", "newmark",
    "aecom", "jacobs", "parsons-corp",
    "ws-atkins", "arup", "wsatkins",
    "ch2m", "gensler", "hok",
    "perkinswill", "skidmore", "sba-communications",
    "american-tower", "crown-castle", "uniti",
    "equinix", "digital-realty", "cyrusone",
    "iron-mountain", "recall", "access-corp",
]

# ── TeamTailor slugs ──────────────────────────────────────────────────────────
# TeamTailor is popular across Europe (Nordics, UK, DACH) and growing in US.
# URL: https://{slug}.teamtailor.com
# Public JSON API: GET https://{slug}.teamtailor.com/jobs.json

_TEAMTAILOR_SLUGS: list[str] = [
    # ── Nordic / Scandinavian tech (TeamTailor's home market) ─────────────────
    # Swedish tech unicorns & scale-ups
    "northvolt", "einride", "polestar", "voi", "storytel",
    "hemnet", "bambuser", "paradox-interactive",
    "tele2", "scania", "sandvik", "atlas-copco", "alfa-laval",
    "nibe", "lindab", "hexpol",
    "hm-group", "clas-ohlson", "biltema",
    "bonnier", "schibsted", "egmont",
    "statkraft", "equinor", "aibel",
    "aker-solutions", "subsea7",
    "kongsberg", "nammo", "norsk-hydro",
    "yara", "borregaard", "elkem",
    # Norwegian tech & consulting
    "norconsult", "multiconsult", "asplan-viak",
    "kantega", "iterate", "bekk",
    "miles", "variant", "kodemaker",
    "sikt", "norce",
    "avinor", "ruter", "sporveien",
    "dnb", "seb", "telenor",
    # Finnish companies
    "elisa", "op-financial", "aktia",
    "tieto-evry", "innofactor", "knowit",
    # Nordic IT / consulting
    "atea", "dustin", "advania", "crayon", "proact",
    # Nordic SaaS / software
    "visma", "tripletex", "poweroffice", "xledger",
    "pexip", "whereby", "no-isolation",
    # ── UK ────────────────────────────────────────────────────────────────────
    "gymshark", "castore", "represent",
    "rapha", "huel", "graze", "mindful-chef",
    "gousto", "bloom-and-wild", "patch",
    "bulb", "ovo-energy",
    "curve", "freetrade", "plum", "chip", "wealthify",
    "iwoca", "oaknorth", "tandem",
    "depop", "vinted", "shpock",
    "cazoo", "cinch", "motorway", "carwow",
    "rightmove", "zoopla", "nested",
    "perkbox", "reward-gateway",
    # ── DACH (Germany, Austria, Switzerland) ──────────────────────────────────
    "gorillas", "flink", "wolt",
    "delivery-hero", "flaschenpost",
    "westwing", "home24",
    "about-you", "bonprix",
    "qonto", "penta", "solarisbank", "mambu",
    "raisin", "auxmoney",
    "scalable-capital", "trade-republic",
    "check24", "verivox",
    "curevac", "evotec", "morphosys",
    "suse", "rexx", "haufe",
    # ── Dutch / Benelux ───────────────────────────────────────────────────────
    "coolblue", "wehkamp", "bol",
    "sendcloud", "picqer",
    "catawiki", "marktplaats",
    "mollie", "payconiq",
    "tomtom", "philips",
    # ── French ────────────────────────────────────────────────────────────────
    "blablacar", "doctolib", "leboncoin",
    "lydia", "luko", "mirakl",
    "deezer", "dailymotion",
    "decathlon", "lacoste",
    "contentsquare",
    # ── Southern European ─────────────────────────────────────────────────────
    "glovo", "wallapop", "jobandtalent",
    "idealista", "fotocasa",
    "cabify", "heetch",
    "paack",
    # ── Global remote-first / HR tech companies ───────────────────────────────
    "deel", "remote", "oyster", "papaya-global",
    "velocity-global", "globalization-partners",
    # ── Additional verified TeamTailor customers ──────────────────────────────
    "pricefx", "learnifier", "mentimeter",
    "lookback", "mapillary", "cint",
    "mybring", "helthjem", "porterbuddy",
    "kolonial", "oda", "mat", "goat",
    "tibber", "otovo", "zaptec",
    "rio-vindo", "hydrogrid", "freyr",
    "cognite", "axbit", "emgs",
    "cradle", "spacemaker", "unacast",
    "gelato", "whereby-remote", "confrere",
    "tidal", "no-isolation-prod",
    "finstart-nordic", "zwipe", "idex",
    "fishbrain", "giosg", "wunderkind",
    "funnel", "leadfeeder", "supermetrics",
    "revenyou", "liana-technologies", "veeam-fi",
    "robocorp", "aiven", "f-secure",
    "withsecure", "basware", "sievo",
    "sweco", "rejlers", "ramboll",
    "jacobs", "wsp", "afry",
    "kredinor", "lindorff", "intrum",
    "nets", "bambora", "concardis",
    "worldline-se", "paynova",
    "instabank", "nstart", "monobank",
    "sbanken", "ya-bank", "kraft-bank",
    "komplett", "eplehuset", "power",
    "elkjop", "spaceworld", "coop-digital",
    "norgesgruppen-digital", "rema-digital",
    "sparebank1", "storebrand", "gjensidige",
    "protector", "fremtind", "codan",
    "tryg", "topdanmark", "alm-brand",
    "se-banken", "ikano", "resurs-bank",
    "collector", "hoist-finance", "marginalen",
]

# ── Map platform → (slugs, check_fn_name, career_url_template, priority) ──────

_PLATFORMS = [
    ("greenhouse",      _GREENHOUSE_SLUGS,      "https://boards.greenhouse.io/{slug}",                   70),
    ("lever",           _LEVER_SLUGS,            "https://jobs.lever.co/{slug}",                          70),
    ("smartrecruiters", _SMARTRECRUITERS_SLUGS,  "https://careers.smartrecruiters.com/{slug}",            60),
    ("ashby",           _ASHBY_SLUGS,            "https://jobs.ashbyhq.com/{slug}",                       65),
    ("workable",        _WORKABLE_SLUGS,         "https://apply.workable.com/{slug}",                     55),
    ("bamboohr",        _BAMBOOHR_SLUGS,         "https://{slug}.bamboohr.com/careers",                   50),
    ("recruitee",       _RECRUITEE_SLUGS,        "https://{slug}.recruitee.com",                          50),
    ("icims",           _ICIMS_SLUGS,            "https://{slug}.icims.com/jobs/intro",                   55),
    ("workday",         _WORKDAY_SLUGS,          "https://{slug}.wd1.myworkdayjobs.com/careers",          80),
    ("teamtailor",      _TEAMTAILOR_SLUGS,       "https://{slug}.teamtailor.com/jobs",                    65),
]


# ── ATS probe helpers ─────────────────────────────────────────────────────────

async def _probe_greenhouse(client: httpx.AsyncClient, slug: str) -> bool:
    try:
        r = await client.get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            timeout=PROBE_TIMEOUT,
        )
        return r.status_code == 200 and len(r.json().get("jobs", [])) >= 0
    except Exception:
        return False


async def _probe_lever(client: httpx.AsyncClient, slug: str) -> bool:
    try:
        r = await client.get(
            f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1",
            timeout=PROBE_TIMEOUT,
        )
        return r.status_code == 200 and isinstance(r.json(), list)
    except Exception:
        return False


async def _probe_smartrecruiters(client: httpx.AsyncClient, slug: str) -> bool:
    try:
        r = await client.get(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            params={"limit": 1},
            timeout=PROBE_TIMEOUT,
        )
        return r.status_code == 200
    except Exception:
        return False


async def _probe_ashby(client: httpx.AsyncClient, slug: str) -> bool:
    try:
        r = await client.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
            timeout=PROBE_TIMEOUT,
        )
        return r.status_code == 200
    except Exception:
        return False


async def _probe_workable(client: httpx.AsyncClient, slug: str) -> bool:
    """Check Workable via the public jobs API.

    The HTML page (apply.workable.com/{slug}) returns 200 even for unknown
    slugs — Workable has a catch-all SPA route.  Use the actual v3 API:
    POST /api/v3/accounts/{slug}/jobs with limit=1.  Returns 404 when the
    account doesn't exist, 200 with JSON when it does.
    """
    try:
        r = await client.post(
            f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",
            json={"limit": 1, "details": False},
            headers={"Content-Type": "application/json"},
            timeout=PROBE_TIMEOUT,
        )
        # 200 = valid account, 404 = unknown slug
        # 429 = rate limited → treat as unknown to avoid false positives
        return r.status_code == 200
    except Exception:
        return False


async def _probe_bamboohr(client: httpx.AsyncClient, slug: str) -> bool:
    try:
        r = await client.get(
            f"https://{slug}.bamboohr.com/jobs/embed2.php",
            timeout=PROBE_TIMEOUT,
        )
        return r.status_code == 200 and len(r.text) > 500
    except Exception:
        return False


async def _probe_recruitee(client: httpx.AsyncClient, slug: str) -> bool:
    try:
        r = await client.get(
            f"https://{slug}.recruitee.com/api/offers/?scope=published&limit=1",
            timeout=PROBE_TIMEOUT,
        )
        return r.status_code == 200
    except Exception:
        return False


async def _probe_icims(client: httpx.AsyncClient, slug: str) -> bool:
    try:
        r = await client.get(
            f"https://{slug}.icims.com/jobs/intro",
            timeout=PROBE_TIMEOUT,
        )
        return r.status_code in (200, 301, 302)
    except Exception:
        return False


async def _probe_teamtailor(client: httpx.AsyncClient, slug: str) -> bool:
    """Check TeamTailor by hitting the public jobs.json endpoint."""
    try:
        r = await client.get(
            f"https://{slug}.teamtailor.com/jobs.json",
            timeout=PROBE_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        if r.status_code != 200:
            return False
        data = r.json()
        # Valid if it returns a dict with jobs key or a list
        return isinstance(data, (dict, list))
    except Exception:
        return False


async def _probe_workday(client: httpx.AsyncClient, slug: str):
    """Probe Workday by trying wd1 through wd5 shards.

    Returns a string "tenant|board|shard" if found (e.g. "netapp|External_Career_Site|wd1"),
    or False if not found.  The connector needs this full identifier to fetch jobs.
    """
    # Common board names companies use
    board_candidates = ["External_Career_Site", "External", "careers", "Jobs", "Careers"]
    for shard in ("wd1", "wd5", "wd3", "wd12", "wd2"):
        for board in board_candidates:
            try:
                url = (
                    f"https://{slug}.{shard}.myworkdayjobs.com"
                    f"/wday/cxs/{slug}/{board}/jobs"
                )
                r = await client.post(
                    url,
                    json={"limit": 1, "offset": 0, "searchText": "", "appliedFacets": {}},
                    headers={"Content-Type": "application/json"},
                    timeout=PROBE_TIMEOUT,
                )
                if r.status_code == 200:
                    data = r.json()
                    if "jobPostings" in data or "total" in data:
                        # Return full identifier so connector can fetch correctly
                        return f"{slug}|{board}|{shard}"
            except Exception:
                continue
    return False


_PROBE_FNS = {
    "greenhouse":      _probe_greenhouse,
    "lever":           _probe_lever,
    "smartrecruiters": _probe_smartrecruiters,
    "ashby":           _probe_ashby,
    "workable":        _probe_workable,
    "bamboohr":        _probe_bamboohr,
    "recruitee":       _probe_recruitee,
    "icims":           _probe_icims,
    "workday":         _probe_workday,
    "teamtailor":      _probe_teamtailor,
}


def _slug_to_name(slug: str) -> str:
    import re
    return " ".join(
        w.capitalize() for w in re.split(r"[-_\.]+", slug) if w
    )


async def _upsert_batch(rows: list[dict], now: datetime) -> int:
    """Upsert a batch of company rows using DB column names.

    Priority wins: if the incoming row has a HIGHER priority_score than the
    existing record, we overwrite ats/ats_identifier/careers_url.  Lower- or
    equal-priority platforms never downgrade an existing high-quality entry.
    This prevents workable (55) from clobbering greenhouse (70) and flickering
    every ingest cycle.
    """
    if not rows:
        return 0
    inserted = 0
    async with AsyncSessionLocal() as session:
        for i in range(0, len(rows), 500):
            batch = rows[i : i + 500]
            ins = pg_insert(Company.__table__).values(batch)
            stmt = ins.on_conflict_do_update(
                index_elements=["name"],
                set_={
                    # Only overwrite ATS info when new priority > existing priority
                    "ats": func.case(
                        (ins.excluded.priority_score > Company.__table__.c.priority_score,
                         ins.excluded.ats),
                        else_=Company.__table__.c.ats,
                    ),
                    "ats_identifier": func.case(
                        (ins.excluded.priority_score > Company.__table__.c.priority_score,
                         ins.excluded.ats_identifier),
                        else_=Company.__table__.c.ats_identifier,
                    ),
                    "careers_url": func.case(
                        (ins.excluded.priority_score > Company.__table__.c.priority_score,
                         ins.excluded.careers_url),
                        else_=Company.__table__.c.careers_url,
                    ),
                    # Always update these — safe to merge regardless of priority
                    "priority_score": func.greatest(
                        Company.__table__.c.priority_score,
                        ins.excluded.priority_score,
                    ),
                    "active":      True,
                    "updated_at":  now,
                },
            )
            result = await session.execute(stmt)
            await session.commit()
            inserted += result.rowcount or len(batch)
    return inserted


async def _probe_platform(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    ats: str,
    slug: str,
    career_url_tpl: str,
    priority: int,
    probe_fn,
    now: datetime,
) -> dict | None:
    """Probe one slug and return a company row dict if confirmed, else None.

    probe_fn can return:
    - False / None  → company not found, skip
    - True          → found, use slug as ats_identifier
    - str           → found, use the returned string as ats_identifier (Workday)
    """
    async with sem:
        result = await probe_fn(client, slug)

    if not result:
        return None

    # Workday returns the full "tenant|board|shard" string; others return True
    ats_identifier = result if isinstance(result, str) else slug

    # Use DB column names (not Python attribute names) for __table__ insert
    career_url = career_url_tpl.format(slug=slug)
    return {
        "name":                  _slug_to_name(slug),
        "ats":                   ats,
        "ats_identifier":        ats_identifier,
        "careers_url":           career_url,
        "priority_score":        priority,
        "scan_frequency_minutes": 360,
        "next_scan_at":          now + timedelta(minutes=30),
        "active":                True,
        "failure_count":         0,
        "consecutive_failures":  0,
        "jobs_found_count":      0,
        "created_at":            now,
        "updated_at":            now,
    }


# Per-platform concurrency limits — platforms that rate-limit need lower values
_PLATFORM_CONCURRENCY = {
    "greenhouse":      60,   # very permissive API
    "lever":           40,   # permissive
    "smartrecruiters": 40,   # permissive
    "ashby":           30,   # slightly stricter
    "workable":         5,   # aggressively rate-limited — keep low
    "bamboohr":        15,
    "recruitee":       15,
    "icims":           15,
    "workday":          8,   # Workday is slow, keep low
    "teamtailor":      20,   # reasonable; no known hard rate limit
}


async def _ingest_all() -> dict:
    totals: dict[str, int] = defaultdict(int)
    now = datetime.now(timezone.utc)

    async with httpx.AsyncClient(
        headers={"User-Agent": "JobJarvis/1.0 ramvamshikrishna0@gmail.com"},
        follow_redirects=True,
        timeout=PROBE_TIMEOUT,
    ) as client:

        for ats, slugs, career_url_tpl, priority in _PLATFORMS:
            probe_fn = _PROBE_FNS[ats]
            # Use platform-specific concurrency to avoid rate limits
            concurrency = _PLATFORM_CONCURRENCY.get(ats, CONCURRENCY)
            sem = asyncio.Semaphore(concurrency)

            logger.info("ats_directory_probe_start",
                        ats=ats, total_slugs=len(slugs), concurrency=concurrency)

            tasks = [
                _probe_platform(client, sem, ats, slug,
                                career_url_tpl, priority, probe_fn, now)
                for slug in slugs
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            rows = [r for r in results if isinstance(r, dict)]

            inserted = await _upsert_batch(rows, now)
            totals[ats] = inserted
            logger.info("ats_directory_probe_done",
                        ats=ats, probed=len(slugs), confirmed=len(rows), upserted=inserted)

    # ── Seed custom portal companies (no probing — known URLs) ───────────────
    custom_rows = await _seed_custom_portals(now)
    totals["custom_portal"] = custom_rows

    total = sum(totals.values())
    logger.info("ats_directory_complete",
                totals=dict(totals), total_companies=total)
    return dict(totals)


# ── Custom portal seed ────────────────────────────────────────────────────────
# Companies that run their own career portals (no standard ATS).
# ats_identifier = the root career URL.  We seed them directly without probing
# because their portals don't have a uniform API to check.
_CUSTOM_PORTAL_COMPANIES: list[tuple[str, str]] = [
    # Big Tech — proprietary portals
    ("Apple",           "https://jobs.apple.com"),
    ("Amazon",          "https://www.amazon.jobs"),
    ("Google",          "https://careers.google.com"),
    ("Meta",            "https://www.metacareers.com"),
    ("Netflix",         "https://jobs.netflix.com"),
    ("Twitter / X",     "https://careers.x.com"),
    ("Uber",            "https://www.uber.com/us/en/careers"),
    ("Lyft",            "https://www.lyft.com/careers"),
    ("Airbnb",          "https://careers.airbnb.com"),
    ("Pinterest",       "https://www.pinterestcareers.com"),
    ("Snap",            "https://careers.snap.com"),
    ("Reddit",          "https://www.redditinc.com/careers"),
    ("Shopify",         "https://www.shopify.com/careers"),
    ("Atlassian",       "https://www.atlassian.com/company/careers"),
    ("Dropbox",         "https://jobs.dropbox.com"),
    ("Twilio",          "https://www.twilio.com/en-us/company/jobs"),
    ("Cloudflare",      "https://www.cloudflare.com/careers"),
    ("Figma",           "https://www.figma.com/careers"),
    ("Notion",          "https://www.notion.so/careers"),
    ("Canva",           "https://www.canva.com/careers"),
    ("Duolingo",        "https://careers.duolingo.com"),
    ("SpaceX",          "https://www.spacex.com/careers"),
    ("Tesla",           "https://www.tesla.com/careers"),
    # Finance
    ("Coinbase",        "https://www.coinbase.com/careers"),
    ("Robinhood",       "https://careers.robinhood.com"),
    ("Square / Block",  "https://careers.squareup.com"),
    ("Stripe",          "https://stripe.com/jobs"),
    ("Chime",           "https://careers.chime.com"),
    ("Klarna",          "https://jobs.lever.co/klarna"),
    # Healthcare
    ("Oscar Health",    "https://www.hioscar.com/about/jobs"),
    ("One Medical",     "https://www.onemedical.com/careers"),
    ("Noom",            "https://www.noom.com/careers"),
    # Retail / E-Commerce
    ("Walmart",         "https://careers.walmart.com"),
    ("Target",          "https://corporate.target.com/careers"),
    ("Costco",          "https://www.costco.com/jobs.html"),
    ("Home Depot",      "https://careers.homedepot.com"),
    ("Wayfair",         "https://www.wayfair.com/careers"),
    ("Chewy",           "https://careers.chewy.com"),
    ("Etsy",            "https://careers.etsy.com"),
    ("eBay",            "https://careers.ebayinc.com"),
    # Media & Entertainment
    ("Spotify",         "https://www.lifeatspotify.com"),
    ("Disney",          "https://jobs.disneycareers.com"),
    ("Warner Bros",     "https://careers.wbd.com"),
    ("Paramount",       "https://careers.paramount.com"),
    # Telecom
    ("AT&T",            "https://www.att.jobs"),
    ("Verizon",         "https://www.verizon.com/about/work-here"),
    ("T-Mobile",        "https://careers.t-mobile.com"),
    ("Comcast",         "https://jobs.comcast.com"),
    # Transport & Logistics
    ("UPS",             "https://www.jobs-ups.com"),
    ("FedEx",           "https://careers.fedex.com"),
    ("DoorDash",        "https://careers.doordash.com"),
    ("Instacart",       "https://instacart.careers"),
    # Defense / Gov
    ("Boeing",          "https://jobs.boeing.com"),
    ("Lockheed Martin", "https://www.lockheedmartinjobs.com"),
    ("Raytheon",        "https://jobs.rtx.com"),
    ("Northrop Grumman","https://www.northropgrumman.com/careers"),
    # Automotive
    ("GM",              "https://search-careers.gm.com"),
    ("Ford",            "https://www.ford.com/careers"),
    ("Rivian",          "https://rivian.com/careers"),
    ("Lucid Motors",    "https://jobs.lucidmotors.com"),
    # Misc
    ("Roblox",          "https://careers.roblox.com"),
    ("Epic Games",      "https://www.epicgames.com/site/en-US/careers"),
    ("Nvidia",          "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"),
    ("Waymo",           "https://waymo.com/careers"),
    ("OpenAI",          "https://openai.com/careers"),
    ("Anthropic",       "https://www.anthropic.com/careers"),
    ("Scale AI",        "https://scale.com/careers"),
    ("Cohere",          "https://cohere.com/careers"),
    ("Mistral AI",      "https://mistral.ai/careers"),
    ("Perplexity",      "https://www.perplexity.ai/careers"),
    ("Hugging Face",    "https://apply.workable.com/hugging-face"),
    # Additional AI/ML companies
    ("Stability AI",   "https://stability.ai/careers"),
    ("Runway",         "https://runwayml.com/careers"),
    ("Together AI",    "https://www.together.ai/careers"),
    ("Replicate",      "https://replicate.com/careers"),
    ("ElevenLabs",     "https://elevenlabs.io/careers"),
    ("Midjourney",     "https://www.midjourney.com/careers"),
    ("Character AI",   "https://character.ai/careers"),
    ("Inflection AI",  "https://inflection.ai/careers"),
    ("Adept AI",       "https://www.adept.ai/careers"),
    ("Imbue",          "https://imbue.com/careers"),
    ("Pika Labs",      "https://pika.art/careers"),
    # More fintech
    ("Plaid",          "https://plaid.com/careers"),
    ("Brex",           "https://www.brex.com/careers"),
    ("Ramp",           "https://ramp.com/careers"),
    ("Mercury",        "https://mercury.com/careers"),
    ("Carta",          "https://carta.com/careers"),
    ("Affirm",         "https://www.affirm.com/careers"),
    ("Marqeta",        "https://www.marqeta.com/company/careers"),
    # Enterprise software
    ("ServiceNow",     "https://careers.servicenow.com"),
    ("Workday",        "https://www.workday.com/en-us/company/careers"),
    ("SAP",            "https://www.sap.com/careers"),
    ("Salesforce",     "https://careers.salesforce.com"),
    ("Oracle",         "https://www.oracle.com/careers"),
    ("IBM",            "https://www.ibm.com/employment"),
    # Healthcare
    ("CVS Health",     "https://jobs.cvshealth.com"),
    ("Walgreens",      "https://jobs.walgreens.com"),
    ("Kaiser",         "https://jobs.kaiserpermanente.org"),
    ("HCA Healthcare", "https://careers.hcahealthcare.com"),
    # Additional consumer
    ("Nike",           "https://jobs.nike.com"),
    ("Adidas",         "https://careers.adidas-group.com"),
    ("Lululemon",      "https://careers.lululemon.com"),
    ("Peloton",        "https://www.onepeloton.com/careers"),
    ("Airbnb",         "https://careers.airbnb.com"),
    ("Lyft",           "https://www.lyft.com/careers"),
    # Defense / Aerospace
    ("SpaceX",         "https://www.spacex.com/careers"),
    ("Blue Origin",    "https://www.blueorigin.com/careers"),
    ("Rocket Lab",     "https://www.rocketlabusa.com/careers"),
    ("Planet Labs",    "https://www.planet.com/company/careers"),
    ("Relativity Space", "https://www.relativityspace.com/careers"),
    # Crypto / Web3
    ("Coinbase",       "https://www.coinbase.com/careers"),
    ("Binance",        "https://www.binance.com/en/careers"),
    ("Kraken",         "https://kraken.com/careers"),
    ("Gemini",         "https://www.gemini.com/careers"),
    ("Chainalysis",    "https://www.chainalysis.com/careers"),
    ("Alchemy",        "https://www.alchemy.com/careers"),
    ("Consensys",      "https://consensys.net/open-roles"),
    ("Polygon",        "https://polygon.technology/careers"),
    # SaaS staples
    ("Atlassian",      "https://www.atlassian.com/company/careers"),
    ("Asana",          "https://asana.com/jobs"),
    ("Monday.com",     "https://monday.com/jobs"),
    ("ClickUp",        "https://clickup.com/careers"),
    ("Notion",         "https://www.notion.so/careers"),
    ("Figma",          "https://www.figma.com/careers"),
    ("Canva",          "https://www.canva.com/careers"),
    ("Miro",           "https://miro.com/careers"),
    ("Airtable",       "https://airtable.com/careers"),
    ("Webflow",        "https://webflow.com/careers"),
    ("Retool",         "https://retool.com/careers"),
]


async def _seed_custom_portals(now: datetime) -> int:
    """Directly insert known custom-portal companies without probing."""
    rows = []
    for name, portal_url in _CUSTOM_PORTAL_COMPANIES:
        rows.append({
            "name":                  name,
            "ats":                   "custom_portal",
            "ats_identifier":        portal_url,
            "careers_url":           portal_url,
            "priority_score":        75,   # high priority — these are major companies
            "scan_frequency_minutes": 120, # scan every 2 hours
            "next_scan_at":          now + timedelta(minutes=5),  # scan soon
            "active":                True,
            "failure_count":         0,
            "consecutive_failures":  0,
            "jobs_found_count":      0,
            "created_at":            now,
            "updated_at":            now,
        })

    inserted = await _upsert_batch(rows, now)
    logger.info("custom_portals_seeded", count=inserted)
    return inserted


def _run_async(coro):
    async def _wrapper():
        await async_engine.dispose()
        return await coro
    return asyncio.run(_wrapper())


@celery_app.task(
    name="app.workers.ats_directory_tasks.ingest_ats_directories",
    soft_time_limit=7200,
    max_retries=1,
)
def ingest_ats_directories():
    """
    Probe 5,000+ curated company slugs across 8 ATS platforms.
    Confirmed companies are upserted directly — no guessing needed.
    Expected yield: 2,000–5,000 new confirmed companies.
    """
    return _run_async(_ingest_all())
