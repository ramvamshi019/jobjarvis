# 🎯 Job Automation System

Automated job search, filtering, AI resume generation, and alert system.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your API keys

# 3. Add your resume
# Edit data/master_resume.txt with your actual resume

# 4. Run pipeline (fetch + filter only)
python scheduler.py

# 5. Launch dashboard
streamlit run app.py
```

## Setup Details

### Required: OpenAI API Key
Get one at https://platform.openai.com/api-keys and add to `.env`.

### Optional: Telegram Alerts
1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot`
2. Copy the token to `.env` as `TELEGRAM_BOT_TOKEN`
3. Message [@userinfobot](https://t.me/userinfobot) → copy your chat ID to `TELEGRAM_CHAT_ID`

## Usage

### Dashboard (recommended)
```bash
streamlit run app.py
```
Click "Run Full Pipeline" in the sidebar. Browse jobs, download resumes, track status.

### CLI — One-time run
```bash
python scheduler.py --resumes --alerts
```

### CLI — Continuous monitoring (every 30 min)
```bash
python scheduler.py --loop --interval 30 --resumes --alerts
```

## Architecture

| Module | File | Purpose |
|--------|------|---------|
| Fetch | `fetch_jobs.py` | Async scraping of Greenhouse & Lever APIs |
| Filter | `filter_jobs.py` | Title matching, dedup, scoring (0-100) |
| Database | `db.py` | SQLite storage & queries |
| AI Resume | `generate_resume.py` | GPT-powered ATS resume tailoring |
| PDF | `pdf_generator.py` | ReportLab PDF creation |
| Alerts | `notifier.py` | Telegram bot notifications |
| Pipeline | `pipeline.py` | Full orchestration |
| Dashboard | `app.py` | Streamlit web UI |
| Scheduler | `scheduler.py` | CLI runner with loop mode |

## Adding Companies

Edit `config.py` and add board tokens to `GREENHOUSE_BOARDS` or `LEVER_BOARDS`.
Find board tokens from company career page URLs:
- Greenhouse: `boards.greenhouse.io/{token}`
- Lever: `jobs.lever.co/{token}`

## Safety
- Respects rate limits (configurable)
- Does NOT auto-submit applications
- Does NOT bypass CAPTCHAs
- All resume content is truthful (based on your master resume)
