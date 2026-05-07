#!/usr/bin/env python3
"""
Bulk ATS company discovery script.
Probes Greenhouse, Lever, and Ashby APIs in parallel to find active job boards.
Also fetches Y Combinator company list from their public API.

Usage (copy into container first):
  docker compose cp backend/scripts/discover_companies.py backend:/app/scripts/discover_companies.py
  docker compose exec backend python scripts/discover_companies.py

Options (env vars):
  CONCURRENCY=60   number of parallel HTTP requests (default 60)
  DRY_RUN=1        print hits without inserting into DB
  ATS=greenhouse   only test greenhouse | lever | ashby
"""
import asyncio
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.database import AsyncSessionLocal
from app.models.company import Company

CONCURRENCY = int(os.environ.get("CONCURRENCY", 60))
DRY_RUN = bool(os.environ.get("DRY_RUN", ""))
ATS_FILTER = os.environ.get("ATS", "").lower()  # "greenhouse" | "lever" | "ashby" | "" (all)

# ─── Known candidate slugs ────────────────────────────────────────────────────
# Curated list of tech/startup company slugs to probe
KNOWN_SLUGS = [
    # AI / ML / LLM
    "cohere", "anthropic", "scale-ai", "huggingface", "weights-biases",
    "anyscale", "together-ai", "mistralai", "elevenlabs", "deepgram",
    "speechify", "d-id", "synthesis-ai", "inworld", "groq", "cerebras",
    "sambanova", "graphcore", "hailo", "mosaic", "mosaicml", "aleph-alpha",
    "ai21labs", "banana-dev", "modal", "replicate", "baseten", "bentoml",
    "determined-ai", "clear-ml", "cnvrg-io", "valohai", "modelbit",
    "superwise", "arize-ai", "arthur", "whylabs", "fiddler", "truera",
    "evidently", "gantry", "lakera", "robust-intelligence", "invariant",
    "patronus-ai", "galileo", "humanloop", "brainerd", "scale",
    "labelbox", "scale-ai", "appen", "surge-ai", "snorkel", "aquant",
    "clarifai", "roboflow", "landing-ai", "v7labs", "encord", "hasty",
    "dataloop", "superannotate", "scale-data-engine",
    # Data / Analytics / BI
    "databricks", "snowflake", "dbt-labs", "fivetran", "airbyte",
    "confluent", "mongodb", "elastic", "datadog", "grafana-labs",
    "amplitude", "mixpanel", "heap", "fullstory", "posthog", "segment",
    "rudderstack", "june", "koala", "madkudu", "toplyne", "variance",
    "census", "hightouch", "polytomic", "y42", "grouparoo", "castled",
    "hevo", "stitch", "xplenty", "matillion", "attunity", "talend",
    "informatica", "mulesoft", "boomi", "snaplogic", "tray",
    "workato", "zapier", "make", "n8n", "activepieces",
    "prefect", "dagster", "astronomer", "mage-ai", "kedro", "zenml",
    "tecton", "feast", "hopsworks", "featureform", "fennel",
    "turntable", "transform", "cube", "lightdash", "superset",
    "metabase", "redash", "mode", "sigma", "thoughtspot", "atscale",
    "arctype", "chartio", "cluvio", "holistics", "razorfish",
    "domo", "looker", "sisense", "qlik", "microstrategy", "tableau",
    "yellowfin", "dundas", "birst", "logi-analytics",
    "alation", "atlan", "data-world", "collibra", "informatica",
    "talend", "erwin", "data-catalog", "selectstar", "castor",
    "octopai", "data-lineage", "metaphor-data", "monte-carlo",
    "bigeye", "soda", "re-data", "great-expectations", "lightup",
    "acceldata", "datafold",
    # Dev Tools / Platform / Infra
    "vercel", "netlify", "cloudflare", "fastly", "render",
    "railway", "fly-io", "supabase", "planetscale", "neon",
    "turso", "upstash", "convex", "fauna", "harperdb",
    "cockroachdb", "yugabyte", "citus", "timescale", "questdb",
    "influxdata", "clickhouse", "starburst", "trino",
    "hashicorp", "pulumi", "env0", "spacelift", "atlantis",
    "doppler", "infisical", "vault", "akeyless", "conjur",
    "teleport", "strongdm", "boundary", "twingate", "tailscale",
    "docker", "portainer", "rancher", "suse", "canonical",
    "redhat", "chainguard", "slim", "anchore", "snyk",
    "sonatype", "jfrog", "nexus", "artifactory", "pkg",
    "gradle", "bazel", "earthly", "dagger", "depot",
    "buildkite", "semaphore", "drone", "woodpecker", "circleci",
    "travis-ci", "teamcity", "bamboo", "codefresh", "harness",
    "spinnaker", "argo", "flux", "jenkins",
    "datadog", "newrelic", "dynatrace", "appdynamics", "instana",
    "lightstep", "honeycomb", "observe", "coroot", "groundcover",
    "last9", "signoz", "uptrace", "aspecto", "lumigo",
    "sentry", "bugsnag", "rollbar", "trackjs", "raygun",
    "logrocket", "highlight", "mouseflow", "hotjar", "clarity",
    "contentsquare", "glassbox", "quantum-metric",
    "pagerduty", "opsgenie", "victorops", "squadcast", "signl4",
    "betteruptime", "freshstatus", "statuspage", "atlassian-statuspage",
    "linear", "height", "shortcut", "plane", "huly",
    "notion", "coda", "confluence", "slite", "tettra", "guru",
    "slab", "document360", "gitbook", "readme", "mintlify",
    "swimm", "archbee",
    "github", "gitlab", "bitbucket", "kaleidoscope",
    "sourcegraph", "tabnine", "github-copilot", "codeium",
    "cursor", "replit", "codespaces", "gitpod",
    "postman", "insomnia", "httpie", "hoppscotch",
    "stoplight", "readme", "bump-sh", "redocly",
    "retool", "appsmith", "tooljet", "budibase", "internal",
    "airplane", "windmill", "clutch", "builder-io",
    "webflow", "framer", "squarespace", "wix", "wordpress",
    "ghost", "contentful", "sanity", "strapi", "directus",
    "storyblok", "prismic", "hygraph", "payload", "keystonejs",
    "algolia", "typesense", "meilisearch", "elastic", "solr",
    # Security
    "crowdstrike", "sentinelone", "darktrace", "vectra",
    "cybereason", "illumio", "zscaler", "okta", "auth0",
    "duo", "1password", "bitwarden", "dashlane", "lastpass",
    "lacework", "wiz", "orca", "prisma-cloud", "aqua",
    "styra", "open-policy-agent", "checkov", "tfsec",
    "bridgecrew", "checklist", "indusface", "imperva",
    "akamai", "cloudflare-zero-trust", "perimeter81",
    "cyolo", "appgate", "axis-security", "zpa",
    "tenable", "rapid7", "qualys", "vulcan-cyber",
    "nucleus", "rezilion", "seemplicity", "armorcode",
    "drata", "vanta", "secureframe", "tugboat-logic",
    "anecdotes", "hypercomply", "laika", "sprinto",
    "strike-graph", "scytale", "safebase", "whistic",
    "oneleet", "trustcloud", "conveyor", "thoropass",
    "axio", "bitsight", "securityscorecard", "riskrecon",
    "upguard", "cybergrx", "prevalent", "panorays",
    "abnormal-security", "proofpoint", "mimecast", "inky",
    "tessian", "ironscales", "defendify", "cofense",
    "material-security", "valimail", "agari",
    # FinTech / Payments / Banking
    "stripe", "plaid", "brex", "ramp", "mercury",
    "modern-treasury", "unit", "column", "lithic",
    "highnote", "marqeta", "checkout", "adyen",
    "revolut", "wise", "monzo", "starling", "n26",
    "dave", "current", "varo", "chime", "cash-app",
    "venmo", "zelle", "payoneer", "hyperwallet",
    "nium", "thunes", "terrapay", "currencycloud",
    "airwallex", "silverbird", "banking-circle",
    "rapyd", "modulr", "clearbank", "griffin",
    "treezor", "solarisbank", "mango-pay",
    "klarna", "afterpay", "affirm", "sezzle",
    "zip", "splitit", "paidy", "laybuy",
    "fundbox", "bluevine", "kabbage", "ondeck",
    "clearco", "capchase", "pipe", "river",
    "arc", "rho", "relay", "found",
    "lili", "novo", "bluevine", "grasshopper",
    "treasury-prime", "synctera", "bond", "cambr",
    "solid", "increase", "column-tax", "formance",
    "finix", "checkout-com", "square", "toast",
    "lightspeed", "clover", "shift4", "heartland",
    "nmi", "spreedly", "payroc", "worldpay",
    "fiserv", "fis", "tsys", "nuvei", "paysafe",
    "dlocal", "ebanx", "pagbank", "pagseguro",
    "nubank", "itau-unibanco", "bradesco", "santander",
    "open-banking", "tink", "nordigen", "yapily",
    "truelayer", "finleap", "railsbank", "mambu",
    "temenos", "thought-machine", "10x-banking",
    "finxact", "nymbus", "q2", "finastra", "bankjoy",
    "kasisto", "clinc", "finn-ai", "abe-ai",
    "ocrolus", "inscribe", "heron-data", "codat",
    "plaid", "mx", "yodlee", "finicity", "akoya",
    "quiltt", "method-financial", "array", "atomic",
    "pinwheel", "argyle", "truework", "work-number",
    "enigma", "middesk", "persona", "alloy",
    "socure", "jumio", "onfido", "trulioo",
    "acuant", "mitek", "au10tix", "yoti",
    "clear", "idme", "login-gov", "veratad",
    "threatmetrix", "lexisnexis", "iovation",
    "sardine", "seon", "unit21", "hawk-ai",
    "featurespace", "nice-actimize", "finastra",
    "quantexa", "ayasdi", "brighterion",
    # HR / People / Recruiting
    "rippling", "gusto", "bamboohr", "lattice",
    "culture-amp", "leapsome", "betterworks",
    "15five", "reflektive", "impraise", "engagedly",
    "workleap", "workboard", "small-improvements",
    "keka", "darwinbox", "zoho", "hibob",
    "bob-hr", "factorial", "personio", "spendesk",
    "kenjo", "sagehr", "sage-people", "ceridian",
    "dayforce", "kronos", "ukg", "adp", "paycom",
    "paychex", "paylocity", "isolved", "payroll4free",
    "namely", "justworks", "zenefits", "trinet",
    "insperity", "oraclecloud", "workday",
    "sap-successfactors", "cornerstone", "sumtotal",
    "bridge", "docebo", "absorb", "talent-lms",
    "litmos", "360learning", "lessonly", "trainual",
    "coursera-for-business", "udemy-business",
    "pluralsight", "linkedin-learning", "skillsoft",
    "greenhouse-recruiting", "lever-recruiting",
    "workable", "breezy-hr", "recruitee", "homerun",
    "teamtailor", "pinpoint", "occupop", "comeet",
    "jazz-hr", "newton", "icims", "smartrecruiters",
    "taleo", "kenexa", "silkroad", "cornerstone-ondemand",
    "jobvite", "jobadder", "bullhorn", "vincere",
    "avionte", "crelate", "loxo", "gem",
    "ashby", "dover", "findem", "eightfold",
    "beamery", "avature", "phenom", "paradox",
    "mya", "wade-and-wendy", "jobpal", "olivia",
    "textio", "pymetrics", "hirevue", "codility",
    "hackerrank", "testgorilla", "vervoe", "criteria",
    "sapia-ai", "mettl", "mercer-mettl", "hogan",
    "predictive-index", "caliper", "disc", "16personalities",
    "compa", "pave", "levels-fyi", "option-impact",
    "radford", "imercer", "payscale", "salary-com",
    "bamboo", "secureframe-hr", "remote", "deel",
    "rippling-global", "papaya-global", "velocity",
    "oyster-hr", "multiplier", "remote-first",
    "leapwork", "topia", "move-guides", "benify",
    "benify-flex", "bswift", "businessolver", "workhuman",
    "bonusly", "kazoo", "motivosity", "nectar",
    "kudos", "recognize", "assembly", "cooleaf",
    "compt", "lifeworks", "gympass", "wellable",
    "brightplan", "savvy", "northstar", "enrich",
    "origin", "financial-engines", "edelman-financial",
    "springworks", "darwinbox", "greythr",
    # Legal / Compliance / RegTech
    "ironclad", "clio", "litera", "lawgeex",
    "luminance", "kira", "thoughtriver", "lexion",
    "spotdraft", "contractbook", "juro", "concord",
    "docusign", "hellosign", "pandadoc", "adobe-sign",
    "one-span", "signix", "right-signature",
    "agiloft", "icertis", "apttus", "salesforce-cpq",
    "conga", "nintex", "m-files", "imanage",
    "netdocuments", "worldox", "opentext-legal",
    "aderant", "thomsonreuters", "lexisnexis-legal",
    "westlaw", "practicallaw", "wolterskluwer",
    "navex", "ethicspoint", "riskonnect",
    "grc-software", "roper-technologies", "resolver",
    "quantivate", "metricstream", "servicenow-grc",
    "archer", "openpage", "logicgate",
    "verint", "nice", "workiva", "wolterskluwer",
    "diligent", "board-effect", "govshare",
    "nasdaq-governance", "intralinks", "ansarada",
    "merrill-datasite", "dataroom-inc", "firmex",
    # Healthcare / BioTech / MedTech
    "ro-health", "hims", "noom", "hinge-health",
    "sword-health", "calibrate", "bold", "brightline",
    "lyra", "spring-health", "cerebral", "grow-therapy",
    "headway", "simplepractice", "therapybrands",
    "epocrates", "doximity", "solutionreach",
    "modernizing-medicine", "eclinicalworks", "athenahealth",
    "drchrono", "practice-fusion", "kareo", "nuvolo",
    "curogram", "relatient", "kyruus", "phynd",
    "nuance", "m-modal", "3m-hdd", "dolbey",
    "clinical-architecture", "zynx-health", "wolterskluwer-health",
    "elsevier", "ovid", "ebsco-health",
    "medidata", "veeva", "iqvia", "oracle-health",
    "cerner", "epic-systems", "allscripts", "healtheon",
    "nextgen", "greenway-health", "netsmart",
    "twistle", "notable", "nuance-dragon",
    "tempus", "flatiron", "syapse", "genoptix",
    "foundation-medicine", "guardant-health",
    "exact-sciences", "natera", "invitae",
    "genomic-health", "caris", "myriad",
    "23andme", "ancestry", "nebula",
    "color", "helix", "ambry-genetics",
    "illumina", "pacific-biosciences",
    "oxford-nanopore", "10x-genomics",
    "cellero", "cellentics", "akoya-biosciences",
    "nanostring", "abcellera", "recursion",
    "insitro", "insilico", "exscientia",
    "relay-therapeutics", "schrodinger",
    "atomwise", "benevolent-ai", "healx",
    "aria", "unlearn", "inato", "castor",
    "veeva-vault", "medidata-rave", "oracle-ctms",
    "parexel", "covance", "quintiles", "psi",
    "syneos", "icon", "iqvia-biotech", "covance-drug",
    "labcorp", "quest-diagnostics",
    "biodesix", "prevencio", "cardiomatics",
    "eko", "heartflow", "cardiologs", "cardionxt",
    "veran", "lung-life", "lucira",
    "cue-health", "color-health", "getlabs",
    "sprinter-health", "truepill", "alto",
    "amazon-pharmacy", "pillpack", "dosespot",
    "surescripts", "covermymeds", "rxnt",
    "relay-network", "healthgrades", "zocdoc",
    "doctolib", "patientpoint", "healtheon",
    "castlight", "accolade", "quantum-health",
    "transcarent", "carrum", "vera-whole-health",
    "parsley-health", "forward", "one-medical",
    "iora", "oak-street", "care-more",
    "citymd", "urgent-team", "concentra",
    "medexpress", "nextcare", "american-family-care",
    # E-commerce / Retail / CPG
    "shopify", "bigcommerce", "magento",
    "commercetools", "elastic-path", "fabric",
    "nacelle", "centra", "crystallize",
    "vtex", "netsuite-commerce", "salesforce-commerce",
    "salesforce-b2c", "episerver", "umbraco",
    "kentico", "sitefinity", "sitecore",
    "hybris", "intershop", "demandware",
    "bazaarvoice", "yotpo", "okendo", "stamped",
    "loox", "judge-me", "reviews-io",
    "trustpilot", "reviews", "podium",
    "birdeye", "reputation", "grade-us",
    "cloutly", "brightlocal", "synup",
    "yext", "uberall", "chatmeter",
    "salsify", "akeneo", "inriver", "plytix",
    "stibo", "riversand", "enterworks",
    "centric-software", "enrich", "gepard",
    "syndigo", "gladson", "1worldsync",
    "gdsn", "gs1", "edimax",
    "listrak", "klaviyo", "omnisend", "drip",
    "emarsys", "salesforce-marketing",
    "braze", "iterable", "leanplum",
    "onesignal", "urbanairship", "airship",
    "pushwoosh", "kumulos", "batch",
    "clevertap", "mixpanel-mobile", "amplitude-mobile",
    "appsflyer", "adjust", "branch", "singular",
    "kochava", "tenjin", "tradespark",
    "attentive", "postscript", "smsbump",
    "yotpo-sms", "klaviyo-sms", "recart",
    "tinyclues", "bloomreach", "barilliance",
    "nosto", "certona", "monetate",
    "qubit", "accenture-personalization",
    "richrelevance", "retail-rocket",
    "constructor", "klevu", "searchspring",
    "livesearch", "findify", "tweakwise",
    "hawksearch", "lucidworks", "coveo",
    "algolia", "typesense", "meilisearch",
    "loop-commerce", "loop-returns", "returnly",
    "narvar", "aftership", "route", "malomo",
    "parcellab", "shipbob", "shipmonk",
    "whiplash", "ware2go", "flexe",
    "shipstation", "easypost", "shippo",
    "pirateship", "stamps-com", "endicia",
    "netsuite-wms", "manhattan-associates",
    "blue-yonder", "oracle-wms", "logiwa",
    "deposco", "skubana", "linnworks",
    "brightpearl", "cin7", "orderhive",
    "veeqo", "tradegecko", "unleashed",
    # SaaS / B2B / CRM / Sales
    "salesforce", "hubspot", "pipedrive",
    "outreach", "salesloft", "apollo-io",
    "zoominfo", "demandbase", "6sense",
    "clearbit", "bombora", "g2", "trustradius",
    "peerspot", "gartner-peer-insights",
    "chorus-ai", "gong", "clari", "outplay",
    "amplemarket", "lemlist", "instantly",
    "reply-io", "woodpecker", "mailshake",
    "quickmail", "klenty", "mailmeteor",
    "pandadoc", "proposify", "better-proposals",
    "qwilr", "nusii", "bidsketch",
    "freshsales", "zendesk-sell", "nutshell",
    "close", "streak", "copper", "insightly",
    "nimble", "contactually", "zoho-crm",
    "sugarcrm", "capsule", "vtiger",
    "realvolve", "follow-up-boss", "market-leader",
    "liondesk", "wise-agent", "top-producer",
    "redtail", "wealthbox", "orion",
    "gainsight", "totango", "churnzero",
    "catalyst", "planhat", "natero",
    "akita", "vitally", "staircase-ai",
    "customerio", "intercom", "drift",
    "crisp", "freshdesk", "zendesk",
    "helpscout", "groove", "kayako",
    "kustomer", "gladly", "dixa",
    "gorgias", "reamaze", "tidio",
    "livechat", "tawto", "chaport",
    "olark", "drift", "qualified", "chilipiper",
    "calendly", "acuity", "doodle",
    "youcanbook", "oncehub", "appointlet",
    "savvycal", "cal-com", "tidycal",
    "loom", "descript", "riverside",
    "squadcast", "zencastr", "cleanfeed",
    "streamyard", "restream", "switcher-studio",
    "mmhmm", "prezi-video", "ecamm",
    "zoom", "webex", "teams", "meet",
    "whereby", "livekit", "daily-co",
    "mux", "cloudflare-stream", "api-video",
    "vimeo", "wistia", "sproutvideo",
    "vzaar", "brightcove", "kaltura",
    "vidyard", "boclips", "mediavalet",
    "canto", "bynder", "widen",
    "brandfolder", "frontify", "acquia-dam",
    "extensis", "intelligencebank", "webdam",
    "picturepark", "canto-dam", "orange-dam",
    # Marketing / AdTech / Growth
    "marketo", "pardot", "eloqua",
    "act-on", "autopilot", "activecampaign",
    "hatchbuck", "mailchimp", "campaign-monitor",
    "constant-contact", "aweber", "convertkit",
    "drip", "sendinblue", "moosend",
    "getresponse", "mailerlite", "sendpulse",
    "sendfox", "mailercloud", "sendx",
    "beehiiv", "ghost", "substack",
    "revue", "buttondown", "mailbrew",
    "google-ads", "meta-ads", "tiktok-ads",
    "twitter-ads", "linkedin-ads", "pinterest-ads",
    "snapchat-ads", "reddit-ads", "amazon-ads",
    "the-trade-desk", "mediamath", "appnexus",
    "criteo", "smartly-io", "kenshoo",
    "marin-software", "acquisio", "optmyzr",
    "adzooma", "wordstream", "adstage",
    "triple-whale", "northbeam", "rockerbox",
    "measured", "neustar", "nielsen-marketing",
    "comscore", "lotame", "liveramp",
    "onaudience", "audiencestream", "tealium",
    "segment", "mparticle", "lytics",
    "blueconic", "bloomreach-cdp", "salesforce-cdp",
    "adobe-rtcdp", "treasure-data", "simon-data",
    "klaviyo-cdp", "hightouch-cdp", "actioniq",
    "amperity", "exponea", "bloomreach-cdp",
    "contentsquare", "optimizely", "ab-tasty",
    "vwo", "convert", "statsig",
    "split", "launchdarkly", "growthbook",
    "flagsmith", "unleash", "flipt",
    "appcues", "userflow", "pendo",
    "chameleon", "intercom-product", "whatfix",
    "walkme", "spekit", "help-scout-knowledge",
    "document360", "zendesk-guide", "freshdesk-solutions",
    "guru", "tettra", "slab",
    "notion-team", "confluence-team", "sharepoint",
    "google-workspace", "microsoft-365",
    "airtable", "smartsheet", "monday-com",
    "asana", "clickup", "basecamp",
    "teamwork", "wrike", "proofhub",
    "nifty", "paymo", "flow", "todoist",
    "any-do", "things-app", "omnifocus",
    "craft", "bear", "ia-writer",
    "obsidian", "roam-research", "logseq",
    "remnote", "mem", "capacities",
    "tana", "heptabase", "fibery",
    # Marketplace / Community / Social
    "faire", "mable", "handshake-b2b",
    "rangegoods", "tundra", "abound",
    "orderchamp", "ankorstore", "creoate",
    "faire-com", "houzz-pro", "thumbtack",
    "taskrabbit", "handy", "homejoy",
    "helpling", "seekangeek", "airtasker",
    "upwork", "toptal", "fiverr", "freelancer",
    "guru-freelance", "peopleperhour", "truelancer",
    "99designs", "designcrowd", "dribbble",
    "behance", "creativemarket", "envato",
    "themeforest", "codecanyon", "mojo-themes",
    "templatemonster", "ui8", "pixelbuddha",
    "producthunt", "betalist", "f6s",
    "angel-co", "crunchbase", "pitchbook",
    "tracxn", "dealroom", "beauhurst",
    "preqin", "prequin", "cb-insights",
    "forrester", "gartner", "idc",
    "451-research", "ovum", "yankee-group",
    # Climate / Clean Energy / Sustainability
    "climeworks", "carbon-engineering",
    "global-thermostat", "carbfix",
    "verdox", "terraform-industries",
    "twelve", "co2-sciences", "ccu-technology",
    "charm-industrial", "calcite", "heirloom",
    "sustaera", "climecology", "coaway",
    "origin-materials", "lanzatech", "synverdure",
    "infinium", "enerkem", "waste-to-energy",
    "sunpower", "first-solar", "enphase",
    "solaredge", "sma-solar", "fronius",
    "sungrow", "huawei-solar", "growatt",
    "tesla-energy", "sonnen", "lg-energy",
    "panasonic-energy", "byd-energy", "catl",
    "northvolt", "solid-power", "quantumscape",
    "sila-nanotechnologies", "amprius", "e-magy",
    "brill-power", "addionics", "morrow-batteries",
    "form-energy", "eos-energy", "invinity",
    "hydrostor", "energy-vault", "gravitricity",
    "cora-air", "joby-aviation", "archer-aviation",
    "lilium", "heart-aerospace", "eviation",
    "ampere", "beta-technologies",
    "rivian", "lucid-motors", "fisker",
    "canoo", "lordstown", "workhorse",
    "proterra", "lion-electric", "xos-trucks",
    "nikola-motor", "hyliion", "ideanomics",
    "aurora-innovation", "torc-robotics",
    "kodiak-robotics", "gatik", "embark",
    "ike-trucking", "plus-ai", "outrider",
    "isee", "starsky-robotics", "argo-ai",
    "motional", "phantom-auto", "foretellix",
    # Education / EdTech
    "coursera", "udemy", "edx", "pluralsight",
    "linkedin-learning", "skillshare", "masterclass",
    "brilliant", "khan-academy", "duolingo",
    "babbel", "rosetta-stone", "busuu",
    "pimsleur", "italki", "preply",
    "cambly", "verbling", "rype",
    "outschool", "synthesis", "prenda",
    "acton", "microschool", "thinker-schools",
    "primer", "khan-academy-kids", "epic",
    "readingeggs", "starfall", "abcmouse",
    "ixl", "st-math", "math-playground",
    "dreambox", "kno2", "imagine-learning",
    "lexia", "achieve3000", "renaissance",
    "schoology", "canvas", "blackboard",
    "moodle", "d2l", "instructure",
    "itslearning", "haiku-learning", "swivl",
    "panopto", "kaltura-education", "echo360",
    "ed-by-cerego", "knowbly", "gomo",
    "articulate", "lectora", "dominknow",
    "elucidat", "ispring", "adapt-learning",
    "h5p", "xapi", "watershed",
    "explorance", "qualtrics-education",
    "campus-labs", "el-squared", "student-voice",
    "ruffalo-noel-levitz", "liaison", "slate",
    "technolutions", "targetx", "ellucian",
    "banner", "colleague", "jenzabar",
    "powerschool", "infinite-campus", "focus",
    "skyward", "alma", "schoology",
    "studentinformation", "aeries", "genesis",
    # Real Estate / PropTech
    "opendoor", "offerpad", "knock",
    "flyhomes", "orchard", "homeward",
    "zavvie", "homelight", "better-com",
    "guaranteed-rate", "loanDepot", "rocket-mortgage",
    "uw-com", "caliber-home-loans", "pennymac",
    "freedom-mortgage", "newrez", "mr-cooper",
    "nationstar", "planet-home-lending",
    "loancore", "americahomekey", "paramount-residential",
    "compass", "redfin", "zillow",
    "realtor-com", "homes-com", "trulia",
    "opcity", "inside-real-estate", "boomtown",
    "kvcore", "sierra-interactive", "chime-crm",
    "lion-desk", "follow-up-boss", "wise-agent",
    "propertybase", "contactually", "top-of-mind",
    "bob-desk", "landlord-vision", "buildium",
    "appfolio", "yardi", "mri-software",
    "entrata", "rent-manager", "propertyware",
    "doorloop", "landlord-studio", "rentec",
    "tenant-cloud", "hemlane", "avail",
    "cozy", "turbotenant", "rentspree",
    "tenantturner", "rently", "showmojo",
    "knock-com", "tour24", "vscreen",
    "matterport", "zillow-3d", "iguide",
    "cloudpano", "kuula", "asteroom",
    "giraffe360", "nodalview", "immoviewer",
    "urbanimmersive", "floorplanner", "roomsketcher",
    "cedreo", "planner5d", "magicplan",
    "cubicasa", "matterport-floor-plan",
    # Travel / Hospitality
    "airbnb", "vrbo", "vacasa",
    "evolve", "turnkey", "pillow",
    "lyric", "sonder", "mint-house",
    "stay-alfred", "domio", "life-house",
    "selina", "outsite", "behere",
    "landing", "june-homes", "placemakr",
    "habyt", "cohabs", "hmlet",
    "lyric-com", "athenian-hotel", "bob-w",
    "oyo-homes", "zoku", "adagio",
    "roost", "nettsworth", "layla",
    "booking", "expedia", "hotels-com",
    "kayak", "trivago", "hoteltonight",
    "priceline", "travelzoo", "secret-escapes",
    "hip-hotel", "mr-mrs-smith", "tablet-hotels",
    "lastminute", "easyjet", "ryanair",
    "wizz-air", "norwegian", "frontier",
    "spirit", "allegiant", "sun-country",
    "breeze", "avelo", "swoop",
    "flyr", "flixbus", "greyhound",
    "busbud", "rome2rio", "checkmybus",
    "omio", "trainline", "raileurope",
    "amadeus", "sabre", "travelport",
    "ita-software", "routehappy", "farelogix",
    "travelfusion", "verteil", "hitit",
    "altea", "airlines-reporting", "arc",
    "tripactions", "navan", "travelperk",
    "brex-travel", "ramp-travel", "center",
    "itilite", "travel-perk", "spotnana",
    "mondee", "mozio", "blacklane",
    "curb", "via", "transit-app",
    "moovit", "citymapper", "ally",
    "lime", "bird", "superpedestrian",
    "tier", "wind", "spin",
    "wheels", "bolt", "voi",
    "dott", "hive", "zipp",
    # Robotics / Hardware / IoT
    "boston-dynamics", "agility-robotics",
    "apptronik", "figure", "1x-technologies",
    "sanctuary-ai", "physical-intelligence",
    "skild-ai", "covariant-ai", "vicarious",
    "embodied-intelligence", "robot-ai",
    "dexterity", "mujoco", "machina-labs",
    "machina-corp", "kindred-systems",
    "bright-machines", "symbio-robotics",
    "vention", "universal-robots", "techman",
    "epson-robotics", "fanuc", "kuka",
    "abb-robotics", "yaskawa", "kawasaki-robotics",
    "staubli", "comau", "nachi",
    "doosan-robotics", "aubo", "rokae",
    "elephant-robotics", "ufactory", "mycobot",
    "franka", "kinova", "robotiq",
    "onrobot", "schunk", "piab",
    "festo", "smc", "norgren",
    "sievert", "aerojet-rocketdyne",
    "blue-origin", "spacex", "rocket-lab",
    "relativity-space", "astra", "firefly",
    "virgin-galactic", "virgin-orbit", "momentus",
    "astroscale", "clearspace", "astrobotics",
    "intuitive-machines", "masten", "orbit-fab",
    "redwire", "maxar", "planet",
    "spire", "iceye", "capella",
    "hawkeye-360", "tomorrow-io", "slingshot",
    "aws-ground-station", "leafspace", "spaceflight",
    "exolaunch", "d-orbit", "momentus",
    "apple", "google", "microsoft",
    "amazon", "meta", "nvidia",
    "intel", "amd", "qualcomm",
    "broadcom", "marvell", "mediatek",
    "samsung", "sk-hynix", "micron",
    "western-digital", "seagate", "kingston",
    "corsair", "crucial", "g-skill",
    "asus", "msi", "gigabyte",
    "evga", "zotac", "palit",
    "sapphire", "xfx", "powercolor",
    "arm-holdings", "risc-v", "esperanto",
    "tenstorrent", "graphcore", "cerebras",
    "groq", "sambanova", "habana-labs",
    "untether-ai", "recogni", "blaize",
    "kneron", "mythic", "numenta",
    "memristor", "crossbar", "gyrfalcon",
    "deeplite", "latent-ai", "neuromorphic",
    "innatera", "brainchip", "intel-loihi",
    "ibm-tru-north", "qualcomm-neuromorphic",
    # Misc tech / unicorns / notable
    "palantir", "figma", "canva",
    "miro", "lucidchart", "whimsical",
    "figjam", "mural", "conceptboard",
    "stormboard", "stickies", "ideaboardz",
    "discord", "slack", "mattermost",
    "matrix", "rocket-chat", "zulipchat",
    "twist", "nozbe", "basecamp",
    "37signals", "hey", "fastmail",
    "proton", "tutanota", "startmail",
    "lavabit", "posteo", "mailbox-org",
    "github", "gitlab", "gitea",
    "sourcehut", "codeberg", "radicle",
    "fossil-scm", "mercurial", "darcs",
    "pijul", "jujutsu", "jj-vcs",
    "helix", "kakoune", "neovim",
    "vim", "emacs", "spacemacs",
    "doom-emacs", "vscode", "atom",
    "sublimetext", "textmate", "bbedit",
    "nova-editor", "brackets", "notepad-plus",
    "jetbrains", "idea", "pycharm",
    "webstorm", "goland", "rider",
    "clion", "appcode", "rubymine",
    "phpstorm", "datagrip", "aqua",
    "cursor", "windsurf", "trae",
    "warp", "fig", "termius",
    "iterm2", "hyper", "alacritty",
    "kitty", "ghostty", "contour",
    "foot", "wezterm", "rio",
    "tabby", "fluent-terminal", "commander-one",
    "forklift", "transmit", "cyberduck",
    "filezilla", "winscp", "beyond-compare",
    "araxis-merge", "kaleidoscope", "p4v",
    "tower", "sourcetree", "fork",
    "gitkraken", "smartgit", "git-cola",
    "gitahead", "sublime-merge", "git-extensions",
    # SaaS growth companies (Greenhouse heavy)
    "asana", "dropbox", "box",
    "docusign", "zendesk", "freshworks",
    "twilio", "sendgrid", "mailgun",
    "postmark", "sparkpost", "socketlabs",
    "mandrill", "pepipost", "sendinblue",
    "moosend", "mailjet", "mailchimp",
    "getresponse", "aweber", "constant-contact",
    "benchmark-email", "mailerlite", "emailoctopus",
    "campaign-monitor", "dotdigital", "exacttarget",
    "silverpop", "responsys", "sailthru",
    "listrak", "blueshift", "marigold",
    "acoustic", "selligent", "emarsys",
    "adestra", "dotmailer", "pure360",
    "communicator", "maileon", "rapidmail",
    "cleverreach", "newsletter2go", "inxmail",
    "evalanche", "mailingwork", "optivo",
    "sendinblue", "mailify", "mailup",
    "contactlab", "createsend", "interspire",
    # Additional well-known companies
    "lyft", "doordash", "instacart",
    "airbnb", "uber", "lyft",
    "grab", "gojek", "rappi",
    "ifood", "delivery-hero", "deliveroo",
    "just-eat", "takeaway", "menulog",
    "uber-eats", "postmates", "doordash",
    "gopuff", "gorillas", "getir",
    "zapp", "jiffy", "flink",
    "rohlik", "picnic", "oda",
    "kolonial", "meny", "matvare",
    "kazidomi", "greenweez", "biocoop",
    "naturalia", "la-vie-claire", "bjorg",
    "paysign", "paymentus", "paymentworks",
    "i2c", "payveris", "paysign",
    "volante", "finastra-payments", "temenos-payments",
    "bottomline", "aptean", "infosys-finacle",
    "oracle-flexcube", "tcs-bancs", "sap-banking",
    "fidelity-national", "fis-global", "jack-henry",
    "ncr", "diebold", "nautilus-hyosung",
    "giesecke-devrient", "idemia", "entrust",
    "hid-global", "assa-abloy", "dormakaba",
    "allegion", "salto", "aperio",
    "kisi", "brivo", "openpath",
    "verkada", "briefcam", "genetec",
    "milestone", "avigilon", "hanwha",
    "axis", "bosch-security", "pelco",
    "hikvision", "dahua", "uniview",
    "vivotek", "mobotix", "arecont",
    "alertme", "canary", "simplisafe",
    "ring", "nest", "arlo",
    "wyze", "eufy", "reolink",
    "amcrest", "swann", "lorex",
    "zmodo", "vivotek-smart", "qnap-surveillance",
    "synology-surveillance", "blue-iris",
    "milestone-xprotect", "genetec-security-center",
    "onssi", "exacqvision", "digifort",
    "qognify", "ipsotek", "intellicheck",
    "evolv", "athena-security", "omnilert",
    "shooter-detection", "gunshot-detect",
    "shotspotter", "gun-violence-archive",
]

# ─── Additional slugs: Fortune 500 / large enterprises ───────────────────────
FORTUNE_SLUGS = [
    # Retail & Consumer
    "walmart", "target", "costco", "homedepot", "lowes", "bestbuy",
    "macys", "nordstrom", "gap", "hm", "zara", "wayfair", "chewy",
    "etsy", "poshmark", "mercari", "depop", "thredup", "stitch-fix",
    "warby-parker", "allbirds", "casper", "purple", "tuft-needle",
    "leesa", "saatva", "brooklyn-bedding",
    # Food & Beverage
    "doordash", "ubereats", "grubhub", "instacart", "gopuff",
    "freshpet", "beyond-meat", "impossible-foods", "oatly", "chobani",
    "blue-bottle", "starbucks", "dunkin", "mcdonalds", "yum-brands",
    "chipotle", "sweetgreen", "tender-loving-care", "panera",
    # Healthcare & Biotech
    "unitedhealth", "anthem", "aetna", "cigna", "humana",
    "cvs-health", "walgreens", "mckesson", "cardinal-health",
    "abbvie", "bristol-myers-squibb", "eli-lilly", "pfizer",
    "johnson-johnson", "merck", "amgen", "regeneron", "biogen",
    "gilead", "vertex", "moderna", "biontech", "novartis", "roche",
    "allergan", "bausch-health", "jazz-pharmaceuticals",
    "incyte", "alnylam", "bluebird-bio", "crispr-therapeutics",
    "beam-therapeutics", "prime-medicine", "intellia",
    "recursion", "insitro", "insilico", "exscientia",
    "tempus", "flatiron", "veracyte", "guardant", "foundation-medicine",
    "grail", "exact-sciences", "invitae", "23andme",
    # Insurance & Finance
    "lemonade", "root-insurance", "hippo", "openly", "branch",
    "oscar-health", "clover-health", "bright-health", "alignment",
    "nerdwallet", "creditkarma", "experian", "transunion", "equifax",
    "affirm", "klarna", "afterpay", "sezzle", "splitit",
    "chime", "varo", "current", "dave", "empower-finance",
    "sofi", "betterment", "wealthfront", "m1-finance", "public",
    "webull", "etoro", "tastytrade", "tradovate",
    "marqeta", "galileo", "lithic", "highnote", "unit",
    "treasury-prime", "synctera", "bond", "bankos",
    "braintree", "recurly", "chargebee", "zuora", "maxio",
    "tipalti", "melio", "bill", "stampli", "airbase",
    "ramp", "expensify", "center", "teampay", "mesh-payments",
    # Real Estate & Proptech
    "opendoor", "offerpad", "knock", "orchard", "perch",
    "compass", "redfin", "zillow", "trulia", "realtor",
    "homesnap", "opcity", "side", "real", "exp-realty",
    "lofty", "sierra-interactive", "follow-up-boss",
    "buildium", "appfolio", "propertyware", "rent-manager",
    "yardi", "mri-software", "resman", "entrata", "realpage",
    "costar", "loopnet", "crexi", "ten-x", "auction-com",
    "vts", "hqo", "equiem", "office-app", "angus-anyware",
    # Transportation & Logistics
    "flexport", "project44", "fourkites", "descartes", "e2open",
    "nuvocargo", "forto", "beacon", "transfix", "convoy",
    "uber-freight", "loadsmart", "coyote", "echo-global",
    "ch-robinson", "xpo", "forward-air", "echo",
    "shipbob", "shipmonk", "whiplash", "deliverr", "radio-flyer",
    "narvar", "loop-returns", "returnly", "happy-returns",
    "veho", "lalamove", "goshare", "dolly", "lugg",
    "joyn", "samsara", "motive", "keeptruckin", "platform-science",
    "trimble", "omnitracs", "isaac", "peoplenet",
    # Energy & Climate
    "sunrun", "vivint-solar", "sunnova", "sunpower",
    "tesla-energy", "enphase", "solarEdge", "solaria",
    "stem", "fluence", "nuvve", "volterra", "electriphi",
    "recurrent-energy", "nextracker", "array-technologies",
    "enercon", "vestas", "siemens-gamesa", "ge-vernova",
    "invenergy", "nextera", "orsted", "bp-alternative",
    "aker-clean-hydrogen", "plug-power", "bloom-energy",
    "fuelcell", "ballard", "nel", "itm-power",
    # Media & Entertainment
    "netflix", "disney", "hulu", "peacock", "paramount",
    "warnermedia", "discovery", "amc-networks", "starz",
    "apple-tv", "amazon-prime-video", "youtube",
    "spotify", "pandora", "soundcloud", "tidal", "deezer",
    "twitch", "discord", "reddit", "tumblr", "medium",
    "substack", "ghost", "wordpress", "wix", "squarespace",
    "canva", "figma", "framer", "webflow", "editor-x",
    "vimeo", "brightcove", "kaltura", "panopto",
    # B2B SaaS (more)
    "salesforce", "hubspot", "marketo", "pardot", "eloqua",
    "outreach", "salesloft", "gong", "chorus", "wingman",
    "clari", "boostup", "aviso", "people-ai", "xactly",
    "spiff", "captivateiq", "performio", "everstage",
    "zoominfo", "clearbit", "apollo-io", "lusha", "hunter",
    "seamless-ai", "cognism", "kaspr", "leadiq", "uplead",
    "bombora", "demandbase", "6sense", "terminus", "rollworks",
    "mutiny", "qualified", "drift", "intercom", "zendesk",
    "freshworks", "helpscout", "kustomer", "dixa", "tidio",
    "liveagent", "re-amaze", "gorgias", "gladly", "khoros",
    # HR Tech (more)
    "workday", "successfactors", "oracle-hcm", "bamboohr",
    "gusto", "justworks", "rippling", "deel", "remote",
    "papaya-global", "oyster-hr", "velocity-global",
    "greenhouse-software", "lever", "ashby", "workable",
    "smartrecruiters", "icims", "taleo", "brassring",
    "jobvite", "jazz-hr", "zoho-recruit", "recruiterbox",
    "hirequest", "breezy-hr", "recruitee", "teamtailor",
    "personio", "hibob", "humaans", "charlie-hr",
    "lattice", "culture-amp", "leapsome", "betterworks",
    "15five", "engagedly", "reflektive", "trakstar",
    "helios", "velocity-eq", "pequity", "pave",
    "levels", "radford", "mercer", "wtwco",
    # Gaming
    "roblox", "unity", "epic-games", "riot-games", "activision",
    "blizzard", "ea", "take-two", "2k-games", "rockstar",
    "ubisoft", "square-enix", "bandai-namco", "konami",
    "sega", "capcom", "nintendo", "sony-interactive",
    "microsoft-gaming", "xbox", "steam", "itch-io",
    "scopely", "zynga", "playtika", "jam-city",
    "superplay", "socialpoint", "plarium", "big-fish",
    "kabam", "glu-mobile", "king", "rovio", "supercell",
    "niantic", "nianticlabs", "innersloth", "among-us",
    # Developer Tools (more)
    "jetbrains", "sublimetext", "atom-editor", "brackets",
    "replit", "codesandbox", "stackblitz", "codepen",
    "glitch", "render", "fly-io", "railway", "northflank",
    "porter", "qovery", "cleavr", "forge", "ploi",
    "buddy-works", "semaphore", "buildkite", "drone",
    "earthly", "dagger", "codemagic", "appcircle",
    "bitrise", "circleci", "github-actions",
    "sonarqube", "sonarcloud", "snyk", "checkmarx",
    "veracode", "contrast-security", "seeker", "sqreen",
    "stackhawk", "probely", "detectify",
    # Data & Analytics (more)
    "amplitude", "mixpanel", "heap", "fullstory", "hotjar",
    "mouseflow", "smartlook", "contentsquare", "decibel",
    "quantum-metric", "medallia", "qualtrics", "surveymonkey",
    "typeform", "jotform", "cognito-forms", "paperform",
    "google-analytics", "adobe-analytics", "piano",
    "segment", "rudderstack", "mparticle", "lytics",
    "census", "hightouch", "polytomic", "grouparoo",
    "airbyte", "fivetran", "stitch", "talend", "matillion",
    "streamsets", "nifi", "kafka", "confluent",
    "databricks", "snowflake", "dbt-labs",
    "looker", "tableau", "powerbi", "thoughtspot",
    "atscale", "kyligence", "dremio", "starburst",
]

# ─── Ashby-specific slugs ─────────────────────────────────────────────────────
# Ashby is a newer ATS growing fast among YC/startup companies
ASHBY_SLUGS = [
    # Well-known Ashby customers
    "openai", "anthropic", "mistral", "cohere", "inflection",
    "perplexity", "character", "adept", "imbue", "together",
    "modal", "replicate", "baseten", "groq", "cerebras",
    "anyscale", "ray-project", "determined-ai",
    "notion", "linear", "loom", "pitch", "miro",
    "figma", "framer", "webflow", "craft", "readwise",
    "arc", "browsercompany", "bezel", "raycast", "cleanshot",
    "retool", "airplane", "internal", "budibase", "appsmith",
    "rocketlane", "toplyne", "june", "june-so", "posthog",
    "lago", "hyperline", "metronome", "orb", "amberflo",
    "brex", "ramp", "mercury", "found", "relay",
    "bench", "pilot", "botkeeper", "decimal",
    "rippling", "deel", "remote", "oyster", "papaya",
    "lattice", "leapsome", "culture-amp", "15five",
    "pave", "pequity", "levels", "comprehensive",
    "ashby", "gem", "dover", "checkr", "sterling",
    "workos", "stytch", "clerk", "auth0", "okta",
    "oso", "permit", "warrant", "cerbos",
    "doppler", "infisical", "akeyless", "hashicorp",
    "temporal", "inngest", "trigger", "windmill",
    "dbt-labs", "evidence", "lightdash", "cube",
    "preset", "hex", "deepnote", "observable",
    "meltano", "airbyte", "fivetran", "estuary",
    "census", "hightouch", "polytomic",
    "neon", "planetscale", "turso", "xata",
    "supabase", "appwrite", "convex", "fauna",
    "weaviate", "qdrant", "milvus", "chroma",
    "helicone", "braintrust", "langfuse", "phoenix",
    "prefect", "dagster", "astronomer", "mage",
    "comet", "neptune", "weights-biases", "clearml",
    "scale", "labelbox", "v7labs", "encord",
    "render", "fly", "railway", "northflank",
    "porter", "qovery", "napkin", "retool",
    "vercel", "netlify", "cloudflare", "fastly",
    "tailscale", "netbird", "twingate", "zscaler",
    "semgrep", "snyk", "socket", "socket-security",
    "chainguard", "sigstore", "in-toto", "anchore",
    "sysdig", "lacework", "wiz", "orca", "prisma-cloud",
    "torq", "tines", "shuffle", "n8n", "make",
    "zapier", "workato", "mulesoft", "boomi",
    "incident-io", "pagerduty", "opsgenie", "victorops",
    "statuspage", "atlassian", "jira", "confluence",
    "shortcut", "height", "plane", "clickup",
    "basecamp", "asana", "monday", "teamwork",
    "airtable", "coda", "notion", "outline",
    "slab", "guru", "document360", "gitbook",
    "mintlify", "readme", "stoplight", "bump-sh",
    "stripe", "braintree", "square", "adyen",
    "checkout", "rapyd", "airwallex", "currencycloud",
    "wise", "payoneer", "tipalti", "trolley",
    "mercury", "brex", "ramp", "jeeves",
    "pleo", "payhawk", "spendesk", "soldo",
    "open", "tide", "starling", "monzo",
    "revolut", "wise-platform", "railsr",
]


async def slug_from_name(name: str) -> list[str]:
    """Derive candidate slugs from a company name."""
    n = name.lower()
    # Remove common suffixes
    for suffix in [" inc", " corp", " ltd", " llc", " technologies",
                   " systems", " solutions", " software", " labs", " ai",
                   " tech", ", inc", ", corp", ", ltd", ".", ","]:
        n = n.replace(suffix, "")
    n = n.strip()
    slugs = []
    # hyphenated
    h = re.sub(r"[^a-z0-9]+", "-", n).strip("-")
    slugs.append(h)
    # no separator
    slugs.append(re.sub(r"[^a-z0-9]+", "", n))
    # with 'ai' suffix removed
    if h.endswith("-ai"):
        slugs.append(h[:-3])
    return list(dict.fromkeys(s for s in slugs if s))


async def fetch_yc_companies(client: httpx.AsyncClient) -> list[dict]:
    """Fetch companies from Y Combinator's public API."""
    companies = []
    page = 1
    print("Fetching Y Combinator companies...")
    while True:
        try:
            r = await client.get(
                "https://api.ycombinator.com/v0.1/companies",
                params={"page": page, "per_page": 100},
                timeout=15,
            )
            if r.status_code != 200:
                break
            data = r.json()
            batch = data.get("companies", [])
            if not batch:
                break
            companies.extend(batch)
            print(f"  YC page {page}: {len(batch)} companies (total {len(companies)})", end="\r")
            page += 1
            if page > 200:  # safety cap — YC returns 25/page, 200 pages = 5000 companies
                break
        except Exception as e:
            print(f"\n  YC fetch error: {e}")
            break
    print(f"\n  YC total: {len(companies)} companies fetched")
    return companies


async def check_greenhouse(client: httpx.AsyncClient, slug: str) -> bool:
    try:
        r = await client.get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            timeout=8,
        )
        if r.status_code == 200:
            return len(r.json().get("jobs", [])) > 0
    except Exception:
        pass
    return False


async def check_lever(client: httpx.AsyncClient, slug: str) -> bool:
    try:
        r = await client.get(
            f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1",
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            return isinstance(data, list) and len(data) > 0
    except Exception:
        pass
    return False


async def check_ashby(client: httpx.AsyncClient, slug: str) -> bool:
    """Check if a company has an active Ashby job board."""
    try:
        r = await client.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            jobs = data.get("jobPostings", [])
            return len(jobs) > 0
    except Exception:
        pass
    return False


async def check_smartrecruiters(client: httpx.AsyncClient, slug: str) -> bool:
    """Check if a company has an active SmartRecruiters job board."""
    try:
        r = await client.get(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            params={"limit": 1},
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            return len(data.get("content", [])) > 0
    except Exception:
        pass
    return False


async def get_existing_slugs(db) -> set[str]:
    """Get all ATS identifiers already in DB."""
    r = await db.execute(select(Company.ats_identifier))
    return {row[0] for row in r.fetchall() if row[0]}


async def insert_company(db, name: str, ats_type: str, slug: str):
    """Insert a newly discovered company, skipping duplicates."""
    stmt = pg_insert(Company).values(
        name=name,
        domain=None,
        ats_type=ats_type,
        ats_identifier=slug,
        country="US",
        priority_score=50,
        scan_frequency_minutes=720,
        next_scan_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        active=True,
    ).on_conflict_do_nothing(index_elements=["name"])
    await db.execute(stmt)


async def run_discovery(candidates: list[tuple[str, str]], ashby_candidates: list[tuple[str, str]] = None):
    """
    candidates: list of (display_name, slug_to_test) for GH + Lever
    ashby_candidates: separate list to test against Ashby only
    """
    sem = asyncio.Semaphore(CONCURRENCY)
    found_gh = []
    found_lv = []
    found_ashby = []
    tested = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "JobJarvis/1.0 company-discovery"},
        follow_redirects=True,
    ) as client:
        async with AsyncSessionLocal() as db:
            existing = await get_existing_slugs(db)
            print(f"Existing slugs in DB: {len(existing)}")

            # Filter already-known
            to_test = [(n, s) for n, s in candidates if s not in existing]
            to_test_ashby = [(n, s) for n, s in (ashby_candidates or []) if s not in existing]
            print(f"Candidates to test: {len(to_test)} (GH+Lever) + {len(to_test_ashby)} (Ashby)")

            found_sr = []

            async def test_one(name: str, slug: str):
                nonlocal tested
                async with sem:
                    results = []
                    if ATS_FILTER in ("", "greenhouse"):
                        if await check_greenhouse(client, slug):
                            results.append(("greenhouse", slug, name))
                    if not results and ATS_FILTER in ("", "lever"):
                        if await check_lever(client, slug):
                            results.append(("lever", slug, name))
                    if not results and ATS_FILTER in ("", "smartrecruiters"):
                        if await check_smartrecruiters(client, slug):
                            results.append(("smartrecruiters", slug, name))
                    tested += 1
                    if tested % 100 == 0:
                        pct = tested / max(len(to_test), 1) * 100
                        print(f"  Progress: {tested}/{len(to_test)} ({pct:.0f}%) | "
                              f"GH={len(found_gh)} LV={len(found_lv)} SR={len(found_sr)} Ashby={len(found_ashby)}", end="\r")
                    return results

            async def test_ashby(name: str, slug: str):
                nonlocal tested
                async with sem:
                    results = []
                    if ATS_FILTER in ("", "ashby"):
                        if await check_ashby(client, slug):
                            results.append(("ashby", slug, name))
                    return results

            # ── Phase 1: Greenhouse + Lever ──────────────────────────────────
            print(f"\nPhase 1: Testing Greenhouse + Lever ({len(to_test)} candidates)...")
            tasks = [test_one(name, slug) for name, slug in to_test]
            batch_size = 500
            for i in range(0, len(tasks), batch_size):
                batch = tasks[i:i + batch_size]
                results = await asyncio.gather(*batch)
                batch_gh, batch_lv, batch_sr = [], [], []
                for hits in results:
                    for ats_type, slug, name in hits:
                        if ats_type == "greenhouse":
                            found_gh.append((name, slug))
                            batch_gh.append((name, slug))
                        elif ats_type == "lever":
                            found_lv.append((name, slug))
                            batch_lv.append((name, slug))
                        elif ats_type == "smartrecruiters":
                            found_sr.append((name, slug))
                            batch_sr.append((name, slug))
                if not DRY_RUN and (batch_gh or batch_lv or batch_sr):
                    for name, slug in batch_gh:
                        await insert_company(db, name, "greenhouse", slug)
                    for name, slug in batch_lv:
                        await insert_company(db, name, "lever", slug)
                    for name, slug in batch_sr:
                        await insert_company(db, name, "smartrecruiters", slug)
                    try:
                        await db.flush()
                    except Exception:
                        await db.rollback()

            # ── Phase 2: Ashby ───────────────────────────────────────────────
            if to_test_ashby and ATS_FILTER in ("", "ashby"):
                tested = 0
                print(f"\n\nPhase 2: Testing Ashby ({len(to_test_ashby)} candidates)...")
                tasks2 = [test_ashby(name, slug) for name, slug in to_test_ashby]
                for i in range(0, len(tasks2), batch_size):
                    batch = tasks2[i:i + batch_size]
                    results = await asyncio.gather(*batch)
                    batch_ashby = []
                    for hits in results:
                        for ats_type, slug, name in hits:
                            found_ashby.append((name, slug))
                            batch_ashby.append((name, slug))
                        tested += 1
                        if tested % 100 == 0:
                            print(f"  Ashby progress: {tested}/{len(to_test_ashby)} | found={len(found_ashby)}", end="\r")
                    if not DRY_RUN and batch_ashby:
                        for name, slug in batch_ashby:
                            await insert_company(db, name, "ashby", slug)
                        try:
                            await db.flush()
                        except Exception:
                            await db.rollback()

            await db.commit()

    total_new = len(found_gh) + len(found_lv) + found_sr.__len__() + len(found_ashby)
    print(f"\n\nDiscovery complete!")
    print(f"  Found on Greenhouse:      {len(found_gh)}")
    print(f"  Found on Lever:           {len(found_lv)}")
    print(f"  Found on SmartRecruiters: {len(found_sr)}")
    print(f"  Found on Ashby:           {len(found_ashby)}")
    print(f"  Total new companies:      {total_new}")
    if DRY_RUN:
        print("\nDRY RUN — nothing inserted. Set DRY_RUN= to insert.")
    return found_gh, found_lv, found_sr, found_ashby


async def main():
    start = time.time()
    print(f"JobJarvis Company Discovery | concurrency={CONCURRENCY} | dry_run={DRY_RUN}")
    print("=" * 60)

    # 1. Build GH+Lever candidates from hardcoded lists
    candidates = [(slug, slug) for slug in KNOWN_SLUGS]
    candidates += [(slug, slug) for slug in FORTUNE_SLUGS]
    seen = set(s for _, s in candidates)

    # 2. Build Ashby-specific candidates (deduplicated)
    ashby_seen = set(ASHBY_SLUGS)
    ashby_candidates = [(slug, slug) for slug in ASHBY_SLUGS]

    # 3. Fetch YC companies and derive slugs for both GH+Lever and Ashby
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        yc_companies = await fetch_yc_companies(client)

    yc_count = 0
    for c in yc_companies:
        name = c.get("name", "")
        if not name:
            continue
        slugs = await slug_from_name(name)
        for slug in slugs[:2]:  # try top 2 variations
            if slug not in seen:
                candidates.append((name, slug))
                seen.add(slug)
                yc_count += 1
            if slug not in ashby_seen:
                ashby_candidates.append((name, slug))
                ashby_seen.add(slug)

    hardcoded_count = len(KNOWN_SLUGS) + len(FORTUNE_SLUGS)
    print(f"Total candidates: {len(candidates)} (GH+Lever) + {len(ashby_candidates)} (Ashby)")
    print(f"  Hardcoded slugs: {hardcoded_count}")
    print(f"  From YC API:     {yc_count}")
    print(f"  Ashby-specific:  {len(ASHBY_SLUGS)}")
    print()

    found_gh, found_lv, found_sr, found_ashby = await run_discovery(candidates, ashby_candidates)

    elapsed = time.time() - start
    print(f"\nTime: {elapsed:.0f}s")

    all_hits = [("greenhouse", n, s) for n, s in found_gh] + \
               [("lever", n, s) for n, s in found_lv] + \
               [("smartrecruiters", n, s) for n, s in found_sr] + \
               [("ashby", n, s) for n, s in found_ashby]
    if all_hits:
        print("\nSample hits (first 30):")
        for ats, name, slug in all_hits[:30]:
            print(f"  {ats}/{slug}  ({name})")


if __name__ == "__main__":
    asyncio.run(main())
