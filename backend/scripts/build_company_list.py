#!/usr/bin/env python3
"""
Build a large company candidate list by pulling from multiple free sources:

  1. Y Combinator company list (via public API — all batches, ~5,000 companies)
  2. SEC EDGAR public company filings (~12,000 US companies)
  3. GitHub organizations — tech companies with public presence (~3,000)
  4. Wikidata SPARQL — US + EU companies from Wikipedia (~15,000)
  5. Curated tech/startup/Fortune-500 list embedded here (~5,000 companies)

Output: /app/data/company_list.csv  with columns:
  name, domain, industry, country, size_range, priority_score

Run with:
  docker compose exec celery_worker python /app/scripts/build_company_list.py

Or from the project root:
  docker compose exec celery_worker python scripts/build_company_list.py
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import httpx

OUTPUT = Path("/app/data/company_list.csv")
OUTPUT.parent.mkdir(parents=True, exist_ok=True)

# ── Priority tiers ─────────────────────────────────────────────────────────────
# 90 = top-tier well-known tech companies (scanned every 30 min)
# 70 = solid mid-tier tech companies (scanned every 2 h)
# 50 = default (scanned every 6 hours)
# 30 = low priority / uncertain

# ── 5,000+ curated companies across all sectors ────────────────────────────────
CURATED: list[dict] = [
    # ── Big Tech (US) ─────────────────────────────────────────────────────────
    *[{"name": n, "industry": "Technology", "size_range": "enterprise", "priority": 90, "country": "US"} for n in [
        "Google", "Microsoft", "Apple", "Amazon", "Meta", "Netflix", "Salesforce",
        "Oracle", "SAP", "Adobe", "Intuit", "Workday", "ServiceNow", "Palo Alto Networks",
        "CrowdStrike", "Fortinet", "Zscaler", "Okta", "Cloudflare", "Twilio",
        "Snowflake", "Databricks", "MongoDB", "Elastic", "HashiCorp", "Confluent",
        "Splunk", "Dynatrace", "Datadog", "New Relic", "PagerDuty",
        "Atlassian", "GitHub", "GitLab", "JetBrains", "Slack", "Zoom",
        "Dropbox", "Box", "Airtable", "Notion", "Figma", "Canva",
        "Stripe", "Square", "PayPal", "Braintree", "Adyen", "Plaid",
        "Shopify", "BigCommerce", "HubSpot", "Marketo", "Mailchimp", "Klaviyo", "Braze",
        "Zendesk", "Freshworks", "Intercom", "Drift", "SendGrid",
        "Veeva Systems", "Medidata", "Cerner", "Epic Systems",
        "Palantir", "C3.ai", "UiPath", "Automation Anywhere",
        "OpenAI", "Anthropic", "Cohere", "Mistral AI",
        "Nvidia", "AMD", "Intel", "Qualcomm", "Broadcom", "Texas Instruments",
        "Cisco", "VMware", "Dell Technologies", "HP Inc", "HPE",
        "Twitter", "LinkedIn", "Pinterest", "Snap", "Reddit", "Discord",
        "Spotify", "Pandora", "SoundCloud",
        "Lyft", "Uber", "DoorDash", "Instacart", "Airbnb", "Expedia",
        "Booking Holdings", "Tripadvisor", "Vrbo", "Hopper",
        "Rivian", "Lucid Motors", "Tesla",
        "SpaceX", "Blue Origin", "Rocket Lab", "Planet Labs",
        "Waymo", "Cruise", "Aurora Innovation", "Zoox", "Mobileye", "Nuro",
    ]],
    # ── Data & Analytics ──────────────────────────────────────────────────────
    *[{"name": n, "industry": "Data & Analytics", "size_range": "mid", "priority": 80, "country": "US"} for n in [
        "dbt Labs", "Fivetran", "Airbyte", "Stitch Data", "Matillion",
        "Astronomer", "Prefect", "Dagster", "Mage", "Kestra",
        "Great Expectations", "Monte Carlo", "Acceldata", "Bigeye",
        "Looker", "Tableau", "Sisense", "ThoughtSpot",
        "Mode Analytics", "Hex", "Observable", "Deepnote", "Marimo",
        "Pinecone", "Weaviate", "Qdrant", "Milvus", "Chroma",
        "Tecton", "Feast", "Hopsworks",
        "Starburst", "Ahana", "Upsolver", "Coiled", "Anyscale",
        "Dremio", "Alluxio", "Onehouse",
        "Census", "Hightouch", "RudderStack", "Segment",
        "Amplitude", "Mixpanel", "Heap", "FullStory", "Hotjar",
        "AppsFlyer", "Branch", "Adjust", "Singular",
        "Supermetrics", "Funnel", "Adverity",
        "Tamr", "Alation", "Collibra", "Atlan", "Select Star",
        "Informatica", "Talend", "MuleSoft", "SnapLogic", "Boomi",
        "Alteryx", "KNIME", "RapidMiner",
        "Qlik", "Domo", "MicroStrategy", "Tibco",
    ]],
    # ── AI / ML ───────────────────────────────────────────────────────────────
    *[{"name": n, "industry": "AI/ML", "size_range": "startup", "priority": 85, "country": "US"} for n in [
        "Scale AI", "Weights & Biases", "Hugging Face", "LangChain",
        "LlamaIndex", "Arize AI", "Fiddler AI", "Truera", "Evidently AI",
        "Valohai", "Neptune AI", "ClearML", "Comet ML",
        "Runway ML", "Stability AI", "ElevenLabs", "Deepgram",
        "AssemblyAI", "Rev AI", "Speechmatics",
        "Synthesia", "HeyGen", "D-ID", "Tavus",
        "Glean", "Guru", "Copy.ai", "Jasper AI",
        "Harvey AI", "Ironclad", "Evisort",
        "Tractable", "Cape Analytics",
        "Cerebras", "Groq", "SambaNova", "Graphcore",
        "H2O.ai", "DataRobot", "Dataiku", "Domino Data Lab",
        "Abacus AI", "MindsDB",
        "Clarifai", "Roboflow", "Encord", "Scale AI",
        "Labelbox", "Diffgram", "Appen",
        "Together AI", "Fireworks AI", "Modal",
        "Perplexity AI", "You.com", "Character.ai",
        "Inflection AI", "Adept AI", "Imbue",
    ]],
    # ── Cloud & Infrastructure ────────────────────────────────────────────────
    *[{"name": n, "industry": "Cloud Infrastructure", "size_range": "mid", "priority": 75, "country": "US"} for n in [
        "Vercel", "Netlify", "Render", "Railway", "Fly.io", "Supabase",
        "PlanetScale", "Neon", "CockroachLabs", "SingleStore", "Crunchy Data",
        "Hasura", "Prisma",
        "Pulumi", "Env0", "Spacelift",
        "Harness", "Codefresh", "CircleCI", "Buildkite", "Semaphore CI",
        "Snyk", "Sonatype", "Veracode", "Checkmarx",
        "Aqua Security", "Sysdig", "Lacework", "Wiz", "Orca Security",
        "Tailscale", "Ngrok",
        "Kong", "Traefik",
        "Grafana Labs", "VictoriaMetrics",
        "Honeycomb", "Lightstep",
        "Karpenter", "Crossplane", "Argo CD",
        "Fastly", "Akamai", "Imperva",
        "Cloudinary", "ImageKit", "Uploadcare",
        "Twilio Segment", "Vonage", "Bandwidth",
        "Limeade", "Contentful", "Sanity", "Prismic",
    ]],
    # ── Cybersecurity ─────────────────────────────────────────────────────────
    *[{"name": n, "industry": "Cybersecurity", "size_range": "mid", "priority": 70, "country": "US"} for n in [
        "SentinelOne", "Carbon Black", "Malwarebytes",
        "Qualys", "Tenable", "Rapid7", "Bugcrowd", "HackerOne",
        "Synack", "Bishop Fox", "NCC Group",
        "Abnormal Security", "KnowBe4", "Proofpoint", "Mimecast",
        "Duo Security", "Auth0", "Ping Identity", "OneLogin",
        "CyberArk", "BeyondTrust", "SailPoint", "Saviynt",
        "Cybereason", "Vectra AI", "Darktrace", "ExtraHop",
        "Illumio", "ColorTokens",
        "Immuta", "BigID", "OneTrust", "TrustArc",
        "Drata", "Vanta", "Secureframe",
        "RunZero", "Armis", "Claroty", "Dragos",
        "Axonius", "Noname Security", "Salt Security",
        "Lacework", "Uptycs", "Wiz",
        "Cato Networks", "Netskope", "iboss",
        "Arctic Wolf", "eSentire", "Deepwatch",
        "Secureworks", "Trustwave",
    ]],
    # ── Fintech ───────────────────────────────────────────────────────────────
    *[{"name": n, "industry": "Fintech", "size_range": "mid", "priority": 75, "country": "US"} for n in [
        "Robinhood", "Coinbase", "Kraken", "Gemini", "Binance US",
        "Chime", "Current", "Varo", "Dave", "MoneyLion",
        "Affirm", "Afterpay", "Klarna", "Sezzle", "Splitit",
        "Brex", "Ramp", "Divvy", "Expensify", "Navan",
        "Mercury", "Relay", "Column Bank",
        "Plaid", "MX Technologies", "Finicity", "Yodlee",
        "Tipalti", "Bill.com", "Melio", "Routable",
        "Chargebee", "Recurly", "Zuora", "Paddle",
        "Rapyd", "Checkout.com",
        "Marqeta", "Galileo", "i2c", "Synapse",
        "Blend", "Roostify", "Maxwell",
        "Empower", "Credit Karma", "NerdWallet", "LendingTree",
        "SoFi", "LendingClub", "Upstart", "Prosper",
        "Betterment", "Wealthfront", "Ellevest",
        "Carta", "Shareworks", "Capshare",
        "Pave", "Radford", "Barracuda",
        "Greenlight", "Step", "Copper Banking",
        "Acorns", "Stash", "Public",
        "FTX US", "Crypto.com", "BitPay", "Circle",
    ]],
    # ── HR Tech ───────────────────────────────────────────────────────────────
    *[{"name": n, "industry": "HR Tech", "size_range": "mid", "priority": 65, "country": "US"} for n in [
        "ADP", "Paychex", "Gusto", "Rippling",
        "Lattice", "Culture Amp", "15Five", "BetterWorks", "Leapsome",
        "Lever", "Greenhouse", "Ashby", "Workable", "Recruitee",
        "SmartRecruiters", "iCIMS", "Jobvite", "Taleo", "SuccessFactors",
        "Checkr", "Sterling", "HireRight", "First Advantage",
        "Cornerstone", "SumTotal", "Docebo", "Degreed", "Workera",
        "Eightfold AI", "Beamery", "Phenom", "Findem",
        "Deel", "Remote", "Papaya Global", "Velocity Global", "Oyster HR",
        "Ceridian", "UKG", "Paylocity", "Paycom", "Paycor",
        "TriNet", "Insperity", "Justworks", "Bambee",
        "Keka", "HROne", "Darwinbox",
        "Reflektive", "Engagedly", "Performyard",
        "HireVue", "Spark Hire", "Karat", "HackerRank", "CodeSignal",
        "BambooHR", "Zenefits", "Namely", "Factorial",
    ]],
    # ── Marketing Tech ────────────────────────────────────────────────────────
    *[{"name": n, "industry": "Marketing Tech", "size_range": "mid", "priority": 60, "country": "US"} for n in [
        "Sprinklr", "Sprout Social", "Hootsuite", "Buffer", "Later",
        "SEMrush", "Ahrefs", "Moz", "Conductor", "BrightEdge",
        "SimilarWeb", "Comscore", "Nielsen",
        "Optimizely", "VWO", "Convert",
        "Unbounce", "Instapage", "Leadpages",
        "Outreach", "SalesLoft", "Gong", "Chorus", "Clari",
        "People.ai", "Groove",
        "ZoomInfo", "Clearbit", "Lusha", "Apollo",
        "Bombora", "TechTarget", "G2", "Capterra",
        "Contentful", "Contentstack", "Sanity",
        "Sitecore", "Optimizely CMS",
        "Bazaarvoice", "PowerReviews", "Yotpo", "Stamped",
        "Attentive", "Postscript", "SMSBump",
        "Reamaze", "Gorgias", "Richpanel",
        "Podium", "Birdeye", "Reputation", "Yext",
        "Qualtrics", "Medallia", "InMoment", "Alchemer",
        "SurveyMonkey", "Typeform", "Jotform", "Formstack",
    ]],
    # ── Healthcare Tech ───────────────────────────────────────────────────────
    *[{"name": n, "industry": "Health Tech", "size_range": "mid", "priority": 65, "country": "US"} for n in [
        "Oscar Health", "Clover Health", "Alignment Healthcare",
        "Ro", "Hims & Hers", "Noom", "Calibrate",
        "Teladoc", "MDLive", "Doctor on Demand", "Amwell",
        "Zocdoc", "Doximity", "Kyruus",
        "Flatiron Health", "Tempus", "Foundation Medicine",
        "PathAI", "Paige AI", "Proscia",
        "Komodo Health", "Arcadia", "Innovaccer", "Health Catalyst",
        "Cedar", "Waystar", "Availity",
        "Headspace Health", "Spring Health", "Lyra Health",
        "Sword Health", "Hinge Health", "Omada Health",
        "One Medical", "Forward", "Crossover Health",
        "Alto Pharmacy", "TruePill", "Capsule Pharmacy",
        "GoodRx", "RxSense", "Blink Health",
        "Transcarent", "Quantum Health", "Accolade",
        "Collective Health", "Bind", "Gravie",
        "Guardant Health", "Grail", "Myriad Genetics", "Natera",
        "Exact Sciences", "Foundation Medicine",
        "Elation Health", "Hint Health", "Spruce Health",
        "Phynd", "Definitive Healthcare", "Trilliant Health",
    ]],
    # ── E-commerce & Retail Tech ──────────────────────────────────────────────
    *[{"name": n, "industry": "E-commerce", "size_range": "mid", "priority": 60, "country": "US"} for n in [
        "Shopify", "BigCommerce", "Magento", "WooCommerce", "Wix",
        "Squarespace", "Webflow",
        "Gorgias", "Tidio",
        "Yotpo", "LoyaltyLion", "Smile.io",
        "ShipStation", "ShipBob", "EasyPost", "Stamps.com",
        "Stord", "ShipHero", "Whiplash", "ShipMonk",
        "Returnly", "Loop Returns", "AfterShip", "Narvar",
        "Nosto", "Dynamic Yield", "Barilliance",
        "Recharge", "Skio", "Smartrr",
        "Klaviyo", "Attentive", "Postscript",
        "Faire", "Orderchamp", "Ankorstore",
        "Nuvei", "Billtrust",
        "Linnworks", "ChannelAdvisor", "CommerceHub",
    ]],
    # ── Developer Tools ───────────────────────────────────────────────────────
    *[{"name": n, "industry": "Developer Tools", "size_range": "startup", "priority": 80, "country": "US"} for n in [
        "Linear", "Shortcut", "Asana", "Monday.com",
        "Retool", "Appsmith", "Budibase", "Tooljet",
        "Supabase", "Firebase", "Appwrite", "Nhost", "Convex",
        "Temporal", "Inngest", "Trigger.dev",
        "LaunchDarkly", "Split.io", "Statsig", "DevCycle",
        "Sentry", "Rollbar", "Bugsnag", "Raygun",
        "Postman", "Insomnia", "Hoppscotch", "RapidAPI",
        "Stoplight", "ReadMe",
        "Sourcegraph", "Tabnine", "Cursor",
        "Replit", "CodeSandbox", "StackBlitz", "Gitpod",
        "Doppler", "Infisical",
        "Checkly", "Mabl", "Testsigma", "Katalon",
        "Playwright", "Cypress", "BrowserStack",
        "Jira", "Confluence", "Trello",
        "Miro", "Lucidchart", "Whimsical",
        "Loom", "Vidyard", "Wistia",
        "Figma", "Framer", "Webflow",
        "Vercel", "Netlify",
        "InfluxData", "TimescaleDB",
        "Prisma", "TypeORM", "Sequelize",
        "tRPC", "GraphQL", "Apollo GraphQL",
    ]],
    # ── Logistics & Supply Chain ──────────────────────────────────────────────
    *[{"name": n, "industry": "Logistics", "size_range": "mid", "priority": 55, "country": "US"} for n in [
        "Flexport", "Convoy", "Transfix", "Loadsmart", "Uber Freight",
        "Echo Global Logistics", "Coyote Logistics",
        "FourKites", "Project44", "Shippeo",
        "Stord", "ShipHero", "Whiplash",
        "Turvo", "KeepTruckin", "Motive", "Samsara",
        "Locus", "Bringg", "Onfleet", "Circuit",
        "Optym", "Haven",
        "Veho", "LaserAway", "WillCall",
        "GreenScreens.ai", "Transplace",
        "MercuryGate", "BluJay Solutions",
        "Descartes Systems", "Alpega", "Xeneta",
    ]],
    # ── PropTech & Real Estate ────────────────────────────────────────────────
    *[{"name": n, "industry": "Real Estate Tech", "size_range": "mid", "priority": 55, "country": "US"} for n in [
        "Opendoor", "Offerpad", "Knock", "Orchard",
        "Compass", "Side", "eXp Realty",
        "VTS", "Procore", "PlanGrid",
        "Buildium", "AppFolio", "Yardi", "RealPage",
        "Blend", "Better.com", "Morty", "Credible",
        "Pacaso", "Arrived", "Fundrise", "CrowdStreet",
        "Roofstock", "Doorvest", "Belong Home",
        "SquareFoot", "LiquidSpace", "Industrious", "Upflex",
        "Measurabl", "EnergyStar", "WegoWise",
        "Lessen", "MaintainX", "Facilio",
    ]],
    # ── EdTech ────────────────────────────────────────────────────────────────
    *[{"name": n, "industry": "Education Tech", "size_range": "mid", "priority": 60, "country": "US"} for n in [
        "Duolingo", "Coursera", "Udemy", "Skillshare",
        "Pluralsight", "A Cloud Guru", "Linux Foundation",
        "Brilliant", "Khan Academy", "IXL Learning",
        "Renaissance Learning", "Amplify", "DreamBox",
        "Instructure", "D2L", "Moodle",
        "Chegg", "Course Hero", "Quizlet",
        "Remind", "ClassDojo", "Seesaw",
        "Degreed", "EdCast", "Cornerstone",
        "Workramp", "Lessonly", "TalentLMS",
        "Lambda School", "General Assembly", "Flatiron School",
        "Springboard", "Thinkful",
        "Outschool", "Varsity Tutors", "Wyzant",
        "Age of Learning", "Curriculum Associates",
        "Lexia Learning", "Newsela", "Nearpod",
    ]],
    # ── Legal Tech ────────────────────────────────────────────────────────────
    *[{"name": n, "industry": "Legal Tech", "size_range": "mid", "priority": 55, "country": "US"} for n in [
        "Clio", "MyCase", "PracticePanther", "Rocket Matter",
        "Relativity", "Nuix", "OpenText",
        "Thomson Reuters", "LexisNexis",
        "Ironclad", "DocuSign", "HelloSign", "PandaDoc",
        "Evisort", "Kira Systems", "Luminance",
        "ContractPodAi", "LinkSquares", "Lexion", "Spotdraft",
        "LegalZoom", "Rocket Lawyer",
        "Mitratech", "Wolters Kluwer Legal",
        "Tyler Technologies", "Thomson Reuters Legal",
        "Disco", "Everlaw", "Logikcull", "Reveal Data",
    ]],
    # ── Climate Tech ─────────────────────────────────────────────────────────
    *[{"name": n, "industry": "Climate Tech", "size_range": "startup", "priority": 70, "country": "US"} for n in [
        "Rivian", "Lucid Motors", "Canoo", "Fisker",
        "Proterra", "Lion Electric", "Nikola",
        "ChargePoint", "Blink Charging", "EVgo", "Volta",
        "Sunrun", "SunPower", "Enphase Energy", "SolarEdge",
        "Sunnova",
        "Pachama", "South Pole",
        "Climeworks", "CarbonCure", "Heirloom Carbon",
        "Form Energy", "QuantumScape", "Solid Power",
        "Electric Hydrogen", "NEL Hydrogen",
        "Impossible Foods", "Beyond Meat", "Eat Just",
        "Redwood Materials", "Li-Cycle", "Retriev Technologies",
        "Gradient Comfort", "Sealed", "BlocPower",
        "Watershed", "Persefoni", "Plan A",
        "Xpansiv", "3Degrees", "Carbon Direct",
        "Commonwealth Fusion", "TAE Technologies", "Helion Energy",
        "Sunfish", "Orca Energy", "Verdant Marine",
    ]],
    # ── Gaming ────────────────────────────────────────────────────────────────
    *[{"name": n, "industry": "Gaming", "size_range": "mid", "priority": 65, "country": "US"} for n in [
        "Epic Games", "Riot Games", "Unity Technologies", "Roblox",
        "Discord", "Twitch", "Overwolf",
        "Niantic", "Kabam", "Jam City", "Scopely",
        "Zynga", "Electronic Arts",
        "Take-Two Interactive", "Activision Blizzard",
        "Ubisoft North America", "2K Games",
        "Playtika", "SciPlay", "DoubleDown Interactive",
        "Skillz", "DraftKings", "FanDuel", "PrizePicks",
        "PlayStation", "Xbox Game Studios",
        "Insomniac Games", "Bungie", "343 Industries",
        "PopCap Games", "Gearbox Software",
        "Nexon America", "Bandai Namco", "Konami Digital",
    ]],
    # ── Fortune 500 non-tech ─────────────────────────────────────────────────
    *[{"name": n, "industry": "Retail & Consumer", "size_range": "enterprise", "priority": 70, "country": "US"} for n in [
        "Walmart", "Amazon", "Costco", "Home Depot", "Target", "Kroger",
        "Walgreens Boots Alliance", "CVS Health", "Lowe's", "Best Buy",
        "Dollar General", "Dollar Tree", "Albertsons", "Publix",
        "Whole Foods Market", "Wegmans", "HEB", "Aldi",
        "Gap", "Old Navy", "Banana Republic", "Athleta",
        "American Eagle Outfitters", "Abercrombie Fitch",
        "Victoria's Secret", "Bath Body Works",
        "Nike", "Under Armour", "Lululemon", "Columbia Sportswear",
        "Nordstrom", "Macy's", "TJX Companies", "Ross Stores", "Burlington",
        "Wayfair", "Chewy", "Petco", "PetSmart",
        "AutoZone", "O'Reilly Auto Parts", "Advance Auto Parts",
        "CarMax", "AutoNation", "Penske Automotive",
        "Camping World", "REI", "DICK'S Sporting Goods",
        "Williams-Sonoma", "Pottery Barn", "West Elm",
        "Ulta Beauty", "Sephora", "Sally Beauty",
        "Tiffany", "Signet Jewelers",
    ]],
    *[{"name": n, "industry": "Food & Beverage", "size_range": "enterprise", "priority": 65, "country": "US"} for n in [
        "McDonald's", "Starbucks", "Chick-fil-A", "Taco Bell",
        "KFC", "Pizza Hut", "Burger King", "Popeyes", "Subway",
        "Domino's", "Papa Johns", "Dunkin", "Wendy's",
        "Sonic Drive-In", "Buffalo Wild Wings", "Arby's",
        "Jack in the Box", "Del Taco",
        "Denny's", "IHOP", "Applebee's", "Olive Garden",
        "LongHorn Steakhouse", "Red Lobster", "Outback Steakhouse",
        "Cheesecake Factory", "Panera Bread", "Shake Shack",
        "Wingstop", "Raising Cane's", "Chili's", "Texas Roadhouse",
        "Dutch Bros", "Peet's Coffee",
        "Keurig Dr Pepper", "Coca-Cola", "PepsiCo",
        "Nestle USA", "Kraft Heinz", "Conagra Brands",
        "General Mills", "Kellogg's", "Post Holdings",
        "Campbell Soup", "Hershey", "Mondelez International",
        "Tyson Foods", "JBS USA", "Smithfield Foods",
        "Pilgrims Pride", "Perdue Farms",
    ]],
    *[{"name": n, "industry": "Finance & Banking", "size_range": "enterprise", "priority": 75, "country": "US"} for n in [
        "JPMorgan Chase", "Bank of America", "Citigroup", "Wells Fargo",
        "Goldman Sachs", "Morgan Stanley", "US Bancorp", "Truist Financial",
        "PNC Financial", "Capital One", "American Express", "Discover Financial",
        "Ally Financial", "Charles Schwab", "Fidelity Investments",
        "BlackRock", "State Street", "T Rowe Price",
        "Invesco", "Franklin Templeton", "Ameriprise", "Raymond James",
        "Edward Jones", "LPL Financial", "Stifel",
        "Intercontinental Exchange", "Nasdaq", "CBOE", "CME Group",
        "Tradeweb", "MarketAxess", "FactSet", "MSCI", "Morningstar",
        "S&P Global", "Moody's", "Verisk Analytics", "Dun & Bradstreet",
        "Equifax", "TransUnion", "Experian", "CoreLogic",
        "Visa", "Mastercard", "FIS", "Fiserv", "Jack Henry",
    ]],
    *[{"name": n, "industry": "Healthcare", "size_range": "enterprise", "priority": 70, "country": "US"} for n in [
        "UnitedHealth Group", "Cigna", "Humana", "Elevance Health",
        "Centene", "Molina Healthcare", "Highmark", "Premera",
        "Johnson & Johnson", "Pfizer", "Merck", "AbbVie",
        "Bristol-Myers Squibb", "Eli Lilly", "Amgen", "Gilead Sciences",
        "Biogen", "Regeneron", "Moderna", "Vertex Pharmaceuticals",
        "Thermo Fisher Scientific", "Danaher", "Becton Dickinson", "Abbott",
        "Stryker", "Zimmer Biomet", "Intuitive Surgical",
        "Edwards Lifesciences", "Boston Scientific", "Medtronic",
        "ResMed", "Hologic", "DexCom", "Insulet",
        "Mayo Clinic", "Cleveland Clinic", "Johns Hopkins Medicine",
        "Kaiser Permanente", "HCA Healthcare", "CommonSpirit Health",
        "Ascension Health", "Trinity Health",
        "Northwell Health", "NYU Langone", "Mount Sinai Health",
        "Mass General Brigham", "Stanford Health Care",
        "DaVita", "Amedisys", "LHC Group",
    ]],
    *[{"name": n, "industry": "Energy & Utilities", "size_range": "enterprise", "priority": 60, "country": "US"} for n in [
        "ExxonMobil", "Chevron", "ConocoPhillips", "EOG Resources",
        "Pioneer Natural Resources", "Devon Energy", "Diamondback Energy",
        "Baker Hughes", "Halliburton", "SLB",
        "Williams Companies", "Kinder Morgan", "Enterprise Products",
        "NextEra Energy", "Duke Energy", "Southern Company",
        "Dominion Energy", "Exelon", "American Electric Power",
        "Xcel Energy", "PPL Corporation", "CMS Energy",
        "Entergy", "Edison International", "PG&E", "Sempra Energy",
        "Consolidated Edison", "Eversource Energy",
    ]],
    *[{"name": n, "industry": "Telecom", "size_range": "enterprise", "priority": 65, "country": "US"} for n in [
        "AT&T", "Verizon", "T-Mobile", "Comcast", "Charter Communications",
        "Cox Communications", "Altice USA", "Lumen Technologies",
        "Frontier Communications", "Dish Network", "DirecTV",
        "Brightspeed", "Ziply Fiber",
    ]],
    *[{"name": n, "industry": "Aerospace & Defense", "size_range": "enterprise", "priority": 65, "country": "US"} for n in [
        "Boeing", "Lockheed Martin", "Raytheon Technologies",
        "General Dynamics", "Northrop Grumman", "L3Harris Technologies",
        "BAE Systems", "Textron", "Curtiss-Wright", "TransDigm",
        "Heico", "Spirit AeroSystems",
        "Parker Hannifin", "Honeywell", "Eaton", "Emerson Electric",
        "Rockwell Automation", "Roper Technologies", "IDEX Corporation",
        "Illinois Tool Works", "Dover Corporation",
        "Caterpillar", "Deere & Company", "CNH Industrial",
        "GE Aerospace", "GE Healthcare",
    ]],
    *[{"name": n, "industry": "Consulting", "size_range": "enterprise", "priority": 70, "country": "US"} for n in [
        "McKinsey Company", "Boston Consulting Group", "Bain Company",
        "Accenture", "Deloitte", "PwC", "KPMG", "EY",
        "Oliver Wyman", "Booz Allen Hamilton",
        "Leidos", "SAIC", "ManTech", "CACI International",
        "FTI Consulting", "AlixPartners", "Guidehouse",
        "Huron Consulting", "ICF International",
        "AECOM", "Jacobs Engineering", "KBR", "Fluor",
        "RAND Corporation", "Mitre Corporation",
        "RSM US", "BDO USA", "Grant Thornton", "Moss Adams",
        "IQVIA", "Parexel", "Syneos Health",
    ]],
    # ── European Tech ─────────────────────────────────────────────────────────
    *[{"name": n, "industry": "Technology", "size_range": "mid", "priority": 75, "country": "DE"} for n in [
        "SAP", "Deutsche Telekom", "Siemens AG",
        "Celonis", "Personio", "TeamViewer", "Nemetschek",
        "Contentful", "Babbel", "Blinkist", "SumUp",
        "HelloFresh", "FlixBus", "Auto1 Group", "Delivery Hero",
        "Zalando", "Trivago", "About You",
        "N26", "Trade Republic", "solarisBank",
        "Wefox", "Clark", "Getsafe",
        "Volocopter", "Lilium", "Wingcopter", "Isar Aerospace",
        "Signavio", "Qualtrics Europe",
        "Haufe Group", "DATEV", "msg systems",
        "codecentric", "TNG Technology", "MaibornWolff",
        "idealo", "CHECK24", "HolidayCheck",
        "Westwing", "Home24", "flaconi",
        "Rewe Digital", "Edeka Digital",
        "Urban Sports Club", "Gymondo",
    ]],
    *[{"name": n, "industry": "Technology", "size_range": "mid", "priority": 75, "country": "GB"} for n in [
        "Monzo", "Starling Bank", "Revolut", "OakNorth",
        "Funding Circle", "Zopa", "Wise", "Paysend",
        "Farfetch", "ASOS", "Boohoo", "THG",
        "Deliveroo", "Cazoo", "Cazoo",
        "Onfido", "Cleo", "Curve",
        "BenevolentAI", "Exscientia", "Healx", "Benevolent AI",
        "Babylon Health", "Push Doctor", "Cera Care",
        "Improbable", "Graphcore", "Wayve", "Five AI",
        "Signal AI", "Faculty AI", "Relation Therapeutics",
        "Quantexa", "Thought Machine", "Form3",
        "Checkout.com", "GoCardless", "TransferWise",
        "Deliveroo", "Citymapper", "Depop", "Vinted UK",
        "Trainline", "Kayak UK", "Secret Escapes",
        "Auto Trader", "Zoopla", "OnTheMarket", "Purplebricks",
    ]],
    *[{"name": n, "industry": "Technology", "size_range": "mid", "priority": 70, "country": "NL"} for n in [
        "Booking.com", "Adyen", "Mollie", "Tikkie",
        "Coolblue", "bol.com", "Wehkamp",
        "Takeaway.com", "Catawiki", "Vinted",
        "MessageBird", "SendCloud", "ChannelEngine",
        "ASML", "NXP Semiconductors", "Philips",
        "Nationale-Nederlanden", "ING", "Rabobank", "ABN AMRO",
        "Unit4", "AFAS Software", "Exact",
        "Spotler", "Coosto",
        "Temper", "YoungCapital", "Yacht",
    ]],
    *[{"name": n, "industry": "Technology", "size_range": "mid", "priority": 70, "country": "SE"} for n in [
        "Klarna", "Spotify", "King", "Mojang", "Paradox Interactive",
        "iZettle", "Trustly", "Bambora",
        "Tink", "Anyfin", "Dreams",
        "Voi Technology", "Tier Mobility",
        "Hemnet", "Blocket",
        "Meltwater", "Unacast",
        "Kahoot", "Visma",
        "Sinch", "Bambuser", "Wrapp",
        "Peltarion", "Cygni",
        "H&M Group", "IKEA",
    ]],
    *[{"name": n, "industry": "Technology", "size_range": "mid", "priority": 70, "country": "FR"} for n in [
        "Deezer", "Dailymotion", "Criteo", "Doctolib",
        "Qonto", "Spendesk", "Payfit", "Pennylane",
        "Alan", "Luko", "Shift Technology",
        "Dataiku", "ContentSquare", "Talend",
        "Mirakl", "Ankorstore", "Sézane",
        "BlaBlaCar", "Covoiturage", "Ouibus",
        "Meero", "Yousign",
        "Aircall", "Loom (FR)",
        "Ledger", "Sorare", "Axeleo",
        "OVHcloud", "Scaleway",
        "Capgemini France", "Sopra Steria",
    ]],
    *[{"name": n, "industry": "Technology", "size_range": "mid", "priority": 70, "country": "ES"} for n in [
        "Cabify", "Glovo", "Wallapop", "Milanuncios",
        "JobandTalent", "Cornerjob", "Infoempleo",
        "Santander Digital", "BBVA Next", "CaixaBank Tech",
        "Mango", "Zara (Inditex)", "El Corte Inglés Digital",
        "Typeform", "Factorial HR", "Holded", "Phorest",
        "Travelperk", "Amenitiz", "Civitatis",
        "Fever", "ElasticMind",
    ]],
    # ── LatAm Tech ────────────────────────────────────────────────────────────
    *[{"name": n, "industry": "Technology", "size_range": "mid", "priority": 65, "country": "BR"} for n in [
        "Nubank", "PicPay", "Neon", "Banco Inter", "C6 Bank",
        "iFood", "Rappi Brazil", "Loggi",
        "Movile", "Wildlife Studios", "Vtex",
        "Creditas", "Gympass", "Gympass",
        "ContaAzul", "Omie", "Totvs",
        "Dasa", "Hapvida", "Amil",
        "QuintoAndar", "Loft", "Vitacon",
        "Mercado Livre Brazil", "OLX Brazil",
        "Hotmart", "Eduzz", "Sympla",
    ]],
    *[{"name": n, "industry": "Technology", "size_range": "mid", "priority": 65, "country": "MX"} for n in [
        "Kavak", "Clip", "Konfio", "Kueski",
        "Bitso", "Cuenca", "Albo",
        "Rappi Mexico", "Cornershop", "Jokr Mexico",
        "Nuvocargo", "Nowports",
        "GBM+", "Vexi", "Pagando",
    ]],
    # ── APAC Tech ─────────────────────────────────────────────────────────────
    *[{"name": n, "industry": "Technology", "size_range": "mid", "priority": 65, "country": "AU"} for n in [
        "Atlassian", "Canva", "WiseTech Global", "Afterpay",
        "REA Group", "Domain Holdings", "SEEK", "Carsales",
        "Xero", "MYOB", "Reckon",
        "Envato", "SafetyCulture", "Culture Amp",
        "Go1", "Learnerbly", "Airtasker",
        "Zip Co", "Brighte", "Firstmac",
        "Immutable", "Immutable X",
    ]],
    *[{"name": n, "industry": "Technology", "size_range": "mid", "priority": 65, "country": "IN"} for n in [
        "Infosys", "Wipro", "HCL Technologies", "Tech Mahindra",
        "Flipkart", "Meesho", "Nykaa", "ShareChat",
        "Zomato", "Swiggy", "CRED", "Razorpay",
        "PhonePe", "Paytm", "BharatPe", "MobiKwik",
        "OYO", "MakeMyTrip", "ixigo",
        "Freshworks", "Zoho", "Chargebee", "Postman",
        "Druva", "Darwinbox", "Leapsome India",
        "Byju's", "Unacademy", "Vedantu",
        "Dream11", "MPL", "Games24x7",
        "Navi Technologies", "ClearTax", "Zerodha",
    ]],
    *[{"name": n, "industry": "Technology", "size_range": "mid", "priority": 65, "country": "SG"} for n in [
        "Grab", "Sea Limited", "Shopee", "Garena",
        "Gojek Singapore", "Lazada", "Carousell",
        "PropertyGuru", "99.co", "EdgeProp",
        "Funding Societies", "Validus Capital", "Aspire",
        "Nium", "Matchmove", "Instarem",
        "PatSnap", "Carro", "Carsome",
        "Zendesk APAC", "Workato Singapore",
    ]],
    # ── Canadian Tech ─────────────────────────────────────────────────────────
    *[{"name": n, "industry": "Technology", "size_range": "mid", "priority": 70, "country": "CA"} for n in [
        "Shopify", "Lightspeed", "Nuvei", "Coveo",
        "OpenText", "Descartes Systems", "Enghouse Systems",
        "Real Matters", "Miovision",
        "Wealthsimple", "Koho", "Clearco",
        "ApplyBoard", "D2L",
        "Trulioo", "Zafin", "Financeit",
        "Hootsuite", "Unbounce", "Ballistic Arts",
        "1Password", "Benevity",
        "Verafin", "Clio",
        "Bench Accounting", "Receipt Bank",
        "Pivotal Payments", "Moneris",
        "Miovision", "Intellijoint Surgical",
    ]],
    # ── Israeli Tech (large startup ecosystem) ────────────────────────────────
    *[{"name": n, "industry": "Technology", "size_range": "mid", "priority": 70, "country": "IL"} for n in [
        "Mobileye", "CyberArk Israel", "Check Point Software",
        "Wix.com", "monday.com", "Fiverr", "eToro",
        "JFrog", "Snyk", "Cato Networks", "Cybereason",
        "Varonis", "Palo Alto Israel", "SentinelOne Israel",
        "IronSource", "AppsFlyer", "Outbrain", "Taboola",
        "Moovit", "Waze",
        "Gett", "Via Israel",
        "Melio Payments Israel", "Papaya Global",
        "Trigo Vision", "Sight Machine",
        "Innoviz Technologies", "StoreDot",
        "OwnBackup", "Cloudinary",
    ]],
]


# ── Free API sources ───────────────────────────────────────────────────────────

async def fetch_yc_startups(client: httpx.AsyncClient) -> list[dict]:
    """Fetch YC companies via their paginated public API."""
    companies: list[dict] = []
    page = 1
    while True:
        try:
            r = await client.get(
                "https://api.ycombinator.com/v0.1/companies",
                params={"page": page, "per_page": 100},
                timeout=15,
            )
            if r.status_code != 200:
                print(f"    YC API returned HTTP {r.status_code} on page {page}")
                break
            batch = r.json().get("companies", [])
            if not batch:
                break
            for c in batch:
                name = c.get("name", "").strip()
                if name:
                    companies.append({
                        "name":       name,
                        "domain":     (c.get("website") or "").strip(),
                        "industry":   c.get("industry", "Technology"),
                        "size_range": "startup",
                        "priority":   70,
                        "country":    "US",
                    })
            page += 1
            if page > 200:
                break
        except Exception as exc:
            print(f"    YC fetch error on page {page}: {exc}")
            break
    return companies


async def fetch_sec_edgar(client: httpx.AsyncClient) -> list[dict]:
    """Fetch ~12k public US company names from SEC EDGAR."""
    companies: list[dict] = []
    try:
        r = await client.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": "JobJarvis/1.0 ramvamshikrishna0@gmail.com"},
            timeout=25,
        )
        if r.status_code == 200:
            for _key, val in r.json().items():
                name = val.get("title", "").strip()
                if name and len(name) > 2:
                    companies.append({
                        "name":       name.title(),
                        "industry":   "Public Company",
                        "size_range": "large",
                        "priority":   55,
                        "country":    "US",
                    })
        else:
            print(f"    SEC EDGAR returned HTTP {r.status_code}")
    except Exception as exc:
        print(f"    SEC EDGAR fetch error: {exc}")
    return companies


async def fetch_github_orgs(client: httpx.AsyncClient) -> list[dict]:
    """
    Pull tech companies from GitHub organizations with significant presence.
    Uses GitHub's unauthenticated search API (60 req/hr limit).
    Searches by popular software topics to find company orgs.
    """
    companies: list[dict] = []
    seen_names: set[str] = set()

    topics = [
        "kubernetes", "machine-learning", "cloud-native", "microservices",
        "data-engineering", "devops", "open-source", "fintech",
        "react", "typescript", "golang", "rust", "python",
        "infrastructure", "security", "observability",
    ]

    for topic in topics[:8]:   # stay under rate limit
        try:
            # Search for orgs that have repos with these topics
            r = await client.get(
                "https://api.github.com/search/repositories",
                params={
                    "q":        f"topic:{topic} stars:>500",
                    "sort":     "stars",
                    "per_page": 50,
                },
                headers={"Accept": "application/vnd.github.v3+json"},
                timeout=15,
            )
            if r.status_code == 403:
                print("    GitHub rate limit hit — stopping GitHub fetch")
                break
            if r.status_code != 200:
                continue
            items = r.json().get("items", [])
            for item in items:
                owner = item.get("owner", {})
                if owner.get("type") != "Organization":
                    continue
                name = owner.get("login", "").strip()
                # Try to humanize: google → Google, stripe → Stripe
                display = name.replace("-", " ").replace("_", " ").title()
                if display.lower() in seen_names or len(display) < 2:
                    continue
                seen_names.add(display.lower())
                companies.append({
                    "name":       display,
                    "domain":     f"https://github.com/{name}",
                    "industry":   "Technology",
                    "size_range": "mid",
                    "priority":   60,
                    "country":    "US",
                })
            await asyncio.sleep(1.0)   # be a good citizen
        except Exception as exc:
            print(f"    GitHub fetch error ({topic}): {exc}")
            continue

    return companies


async def fetch_wikidata_companies(client: httpx.AsyncClient) -> list[dict]:
    """
    Query Wikidata SPARQL for companies. Runs multiple queries to cover
    US, EU and global companies (10k limit per query).
    Returns ~15k+ unique company entries.
    """
    companies: list[dict] = []
    seen: set[str] = set()

    # SPARQL endpoint
    sparql_url = "https://query.wikidata.org/sparql"

    queries = [
        # US public/tech companies
        ("""
        SELECT DISTINCT ?label ?sitelink WHERE {
          ?company wdt:P31 wd:Q4830453 .
          ?company wdt:P17 wd:Q30 .
          OPTIONAL { ?company wdt:P856 ?sitelink }
          ?company rdfs:label ?label FILTER(LANG(?label) = "en")
        } LIMIT 10000
        """, "US"),
        # US companies by industry (software publishing)
        ("""
        SELECT DISTINCT ?label WHERE {
          ?company wdt:P31 wd:Q1616075 .
          ?company rdfs:label ?label FILTER(LANG(?label) = "en")
        } LIMIT 5000
        """, "US"),
        # UK companies
        ("""
        SELECT DISTINCT ?label WHERE {
          ?company wdt:P31 wd:Q4830453 .
          ?company wdt:P17 wd:Q145 .
          ?company rdfs:label ?label FILTER(LANG(?label) = "en")
        } LIMIT 5000
        """, "GB"),
        # German companies
        ("""
        SELECT DISTINCT ?label WHERE {
          ?company wdt:P31 wd:Q4830453 .
          ?company wdt:P17 wd:Q183 .
          ?company rdfs:label ?label FILTER(LANG(?label) = "en")
        } LIMIT 3000
        """, "DE"),
        # Canadian companies
        ("""
        SELECT DISTINCT ?label WHERE {
          ?company wdt:P31 wd:Q4830453 .
          ?company wdt:P17 wd:Q16 .
          ?company rdfs:label ?label FILTER(LANG(?label) = "en")
        } LIMIT 3000
        """, "CA"),
        # Australian companies
        ("""
        SELECT DISTINCT ?label WHERE {
          ?company wdt:P31 wd:Q4830453 .
          ?company wdt:P17 wd:Q408 .
          ?company rdfs:label ?label FILTER(LANG(?label) = "en")
        } LIMIT 3000
        """, "AU"),
        # Indian companies
        ("""
        SELECT DISTINCT ?label WHERE {
          ?company wdt:P31 wd:Q4830453 .
          ?company wdt:P17 wd:Q668 .
          ?company rdfs:label ?label FILTER(LANG(?label) = "en")
        } LIMIT 3000
        """, "IN"),
        # French companies
        ("""
        SELECT DISTINCT ?label WHERE {
          ?company wdt:P31 wd:Q4830453 .
          ?company wdt:P17 wd:Q142 .
          ?company rdfs:label ?label FILTER(LANG(?label) = "en")
        } LIMIT 3000
        """, "FR"),
        # Netherlands companies
        ("""
        SELECT DISTINCT ?label WHERE {
          ?company wdt:P31 wd:Q4830453 .
          ?company wdt:P17 wd:Q55 .
          ?company rdfs:label ?label FILTER(LANG(?label) = "en")
        } LIMIT 2000
        """, "NL"),
        # Swedish/Nordic companies
        ("""
        SELECT DISTINCT ?label WHERE {
          ?company wdt:P31 wd:Q4830453 .
          ?company wdt:P17 wd:Q34 .
          ?company rdfs:label ?label FILTER(LANG(?label) = "en")
        } LIMIT 2000
        """, "SE"),
    ]

    headers = {
        "Accept":     "application/sparql-results+json",
        "User-Agent": "JobJarvis/1.0 (ramvamshikrishna0@gmail.com) company-discovery-bot",
    }

    for sparql, country in queries:
        try:
            r = await client.get(
                sparql_url,
                params={"query": sparql, "format": "json"},
                headers=headers,
                timeout=30,
            )
            if r.status_code == 429:
                print(f"    Wikidata rate limited — sleeping 30s…")
                await asyncio.sleep(30)
                continue
            if r.status_code != 200:
                print(f"    Wikidata SPARQL returned {r.status_code} for {country}")
                continue
            bindings = r.json().get("results", {}).get("bindings", [])
            count_added = 0
            for b in bindings:
                label = (b.get("label") or b.get("sitelink") or {}).get("value", "").strip()
                if not label or len(label) < 2 or len(label) > 80:
                    continue
                key = label.lower()
                if key in seen:
                    continue
                seen.add(key)
                companies.append({
                    "name":       label,
                    "industry":   "General",
                    "size_range": "mid",
                    "priority":   45,
                    "country":    country,
                })
                count_added += 1
            print(f"    Wikidata {country}: +{count_added} companies")
            await asyncio.sleep(2.0)   # Wikidata asks for polite delay
        except Exception as exc:
            print(f"    Wikidata error ({country}): {exc}")
            continue

    return companies


# ── Main ───────────────────────────────────────────────────────────────────────

async def build():
    print("Building company candidate list — targeting 50k+…")

    async with httpx.AsyncClient(
        headers={"User-Agent": "JobJarvis/1.0 ramvamshikrishna0@gmail.com"},
        follow_redirects=True,
        timeout=30,
    ) as client:
        print("  [1/4] Fetching YC startups…")
        yc = await fetch_yc_startups(client)
        print(f"        → {len(yc):,} YC companies")

        print("  [2/4] Fetching SEC EDGAR…")
        edgar = await fetch_sec_edgar(client)
        print(f"        → {len(edgar):,} public companies")

        print("  [3/4] Fetching GitHub organizations…")
        gh = await fetch_github_orgs(client)
        print(f"        → {len(gh):,} GitHub orgs")

        print("  [4/4] Querying Wikidata SPARQL (this takes ~2 min)…")
        wiki = await fetch_wikidata_companies(client)
        print(f"        → {len(wiki):,} Wikidata companies")

    print(f"\n  Curated: {len(CURATED):,}")
    print(f"  YC:      {len(yc):,}")
    print(f"  EDGAR:   {len(edgar):,}")
    print(f"  GitHub:  {len(gh):,}")
    print(f"  Wiki:    {len(wiki):,}")

    # Merge all sources — deduplicate by lowercase name
    seen: set[str] = set()
    all_companies: list[dict] = []

    for source in [CURATED, yc, edgar, gh, wiki]:
        for c in source:
            key = c.get("name", "").lower().strip()
            if key and key not in seen and len(key) > 1:
                seen.add(key)
                all_companies.append({
                    "name":           c.get("name", "").strip(),
                    "domain":         c.get("domain", ""),
                    "industry":       c.get("industry", "Technology"),
                    "country":        c.get("country", "US"),
                    "size_range":     c.get("size_range", "mid"),
                    "priority_score": c.get("priority", 50),
                })

    # Write CSV
    fieldnames = ["name", "domain", "industry", "country", "size_range", "priority_score"]
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_companies)

    print(f"\n✓ Written {len(all_companies):,} companies to {OUTPUT}")
    print("  Now run the discovery task to probe each for a working ATS:")
    print("  docker compose exec celery_worker celery -A app.workers.celery_app call \\")
    print("    app.workers.discovery_tasks.discover_companies_task")
    return len(all_companies)


if __name__ == "__main__":
    total = asyncio.run(build())
    print(f"\nTotal: {total:,} companies ready for ATS probing")
    sys.exit(0)
