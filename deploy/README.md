# JobJarvis — production deployment on Google Cloud

End-to-end runbook to take JobJarvis from your laptop to a 24/7 public URL on
Google Cloud Platform. Total setup time: **~30 minutes**. Cost: **~$13/month**
(or **$0** for the first 90 days using GCP's $300 free credit).

---

## What you'll have at the end

- A single Google Compute Engine VM running everything in Docker
- HTTPS via Let's Encrypt (auto-renewed by Caddy)
- Postgres + Redis + FastAPI + Celery worker + Celery beat + Next.js frontend
- A real public URL like `https://jobjarvis.yourname.dev`

---

## Step 1 — Push your code to GitHub

The VM clones from a git repo. If you haven't already:

```bash
cd ~/Desktop/jobjarvis
git init
git add -A
git commit -m "Initial deploy"
# Create a private repo on github.com first, then:
git remote add origin git@github.com:YOUR_USERNAME/jobjarvis.git
git push -u origin main
```

**IMPORTANT**: Do NOT commit `deploy/.env` (it has secrets). The
`.gitignore` should already exclude it.

---

## Step 2 — Buy a domain (or use a free subdomain)

Cheapest options for a personal project:
- **Namecheap** — `.dev` or `.app` costs ~$10/year
- **Google Domains** (now Squarespace Domains) — ~$12/year
- **Free**: use [Duck DNS](https://www.duckdns.org/) for `yourname.duckdns.org`

You'll need to add an **A record** later pointing your domain to the VM IP.

---

## Step 3 — Create a Compute Engine VM

In the [GCP Console](https://console.cloud.google.com):

1. **Create a new project** (free tier: lifetime e2-micro in `us-central1`,
   `us-east1`, `us-west1`)
2. Enable billing (uses your $300 credit, no charges if you stay in free tier)
3. Go to **Compute Engine → VM instances → Create instance**

Recommended config:

| Setting | Value |
|---|---|
| Name | `jobjarvis` |
| Region | `us-central1` (cheapest + free tier) |
| Zone | `us-central1-a` |
| Machine type | **e2-medium** (2 vCPU, 4 GB) — required for sentence-transformers + postgres + frontend together |
| Boot disk | Ubuntu 24.04 LTS, 30 GB standard persistent disk |
| Firewall | ✅ Allow HTTP traffic, ✅ Allow HTTPS traffic |
| Networking → External IP | **Static** (free if attached, $0.004/h if detached) |

Cost: ~$25/month on `e2-medium`, or **free with $300 credit for ~12 months**.

If you want cheaper:
- `e2-small` ($13/mo) — works but tight on RAM, may swap during embedding
- `e2-micro` (free tier) — too small, won't fit the embedding model

Click **Create**. Note the **External IP** address.

---

## Step 4 — Point your domain at the VM

In your domain registrar's DNS panel, add:

```
Type: A
Host: @         (or 'jobjarvis' for a subdomain)
Value: <YOUR-VM-EXTERNAL-IP>
TTL: 300
```

Verify it propagated:
```bash
dig +short jobjarvis.yourname.dev
# should return your VM's IP within a few minutes
```

---

## Step 5 — SSH into the VM and clone the repo

From the GCP console, click **SSH** next to your VM (opens a browser shell), or:

```bash
gcloud compute ssh jobjarvis --zone us-central1-a
```

Inside the VM:

```bash
sudo apt-get update -y
sudo apt-get install -y git
git clone https://github.com/YOUR_USERNAME/jobjarvis.git
cd jobjarvis
```

(If your repo is private, set up a GitHub deploy key first.)

---

## Step 6 — Configure secrets

```bash
cp deploy/.env.example deploy/.env
nano deploy/.env
```

Fill in:
- `DOMAIN` — the domain you set up in Step 4
- `SECRET_KEY` — generate with `openssl rand -hex 32`
- `POSTGRES_PASSWORD` — generate with `openssl rand -hex 24`
- (optional) `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` — only if you want
  paid AI features. JobJarvis works free without them.

Save with Ctrl-O, Enter, Ctrl-X.

---

## Step 7 — Run the deploy script

```bash
bash deploy/deploy.sh
```

This will:
1. Install Docker (~2 min)
2. Build all images (~6–10 min)
3. Start the stack
4. Wait for healthchecks
5. Print your URL

When it finishes, hit `https://yourdomain.dev` in a browser. Caddy
provisions the Let's Encrypt cert in ~30 seconds the first time.

---

## Step 8 — Backfill data

Inside the VM:

```bash
# 1. Run the company discovery pipeline (~30 min, adds 10k+ companies)
bash backend/scripts/run_all_discovery.sh

# 2. Wait 4–6 hours for Celery to scrape jobs from new companies, then
#    embed all jobs:
docker exec jj_celery_worker python3 -u /app/scripts/backfill_embeddings.py
```

Then sign up via your live URL, drop your resume into `/matches`, and
you're shipping.

---

## Operations cheatsheet

```bash
# View logs (live)
docker compose -f deploy/docker-compose.prod.yml logs -f

# View logs for one service
docker compose -f deploy/docker-compose.prod.yml logs -f backend

# Restart one service
docker compose -f deploy/docker-compose.prod.yml restart backend

# Stop everything
docker compose -f deploy/docker-compose.prod.yml down

# Update after a code change (on the VM)
cd ~/jobjarvis
git pull
docker compose -f deploy/docker-compose.prod.yml up -d --build

# Backup the database
docker exec jj_postgres pg_dump -U jobjarvis jobjarvis | gzip > backup-$(date +%F).sql.gz

# Restore
gunzip -c backup-2026-05-07.sql.gz | docker exec -i jj_postgres psql -U jobjarvis jobjarvis
```

---

## Troubleshooting

**Caddy can't get a cert** — DNS A record hasn't propagated yet, or ports 80/443 aren't open.
Run `dig +short yourdomain.dev` (should return your VM IP) and check GCP firewall rules.

**Frontend shows "Network Error" on resume upload** — the `NEXT_PUBLIC_API_URL` build arg
needs to be `https://yourdomain.dev` not `localhost`. Rebuild:
```bash
docker compose -f deploy/docker-compose.prod.yml up -d --build frontend
```

**Worker keeps restarting** — usually OOM. e2-small (2 GB RAM) is too small once
sentence-transformers loads. Upgrade to e2-medium.

**Postgres password change doesn't take effect** — Postgres data is persisted in a
named volume. To reset, `docker compose down -v` (⚠ **wipes all data**).

---

## Cost summary

| Component | Monthly cost |
|---|---|
| e2-medium VM (2 vCPU, 4 GB) | $24.46 |
| 30 GB SSD persistent disk | $5.10 |
| Static external IP (attached) | $0.00 |
| Egress (assume <100 GB/mo) | ~$0.00 |
| **Total** | **~$30/mo** |
| Minus $300 free credit | **$0 for first ~10 months** |

Switch to e2-small + 20 GB disk to drop to **~$15/mo**.

---

## What about a fancier setup?

If you outgrow this single-VM setup (1000+ users, multiple regions), the next step is:

- Backend → Cloud Run (autoscale 0→100 instances)
- Postgres → Cloud SQL (managed, automated backups)
- Redis → Memorystore
- Worker → stay on a small dedicated VM (model needs to stay warm)
- Frontend → Vercel or Cloud Run

Cost goes from $30/mo → $80–150/mo, but you can serve thousands of users.

Don't optimize for that until you have actual users.
