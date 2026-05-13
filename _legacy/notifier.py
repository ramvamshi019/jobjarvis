"""
Multi-Channel Notification System — Telegram + Email alerts.

Features:
  - Telegram bot notifications with rich formatting
  - Email alerts via SMTP (Gmail, Outlook, custom)
  - Batch digest emails with HTML formatting
  - Daily summary alerts
  - Rate-limit aware sending
  - Graceful degradation when channels are unconfigured
"""

import asyncio
import logging
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

import httpx

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    EMAIL_ENABLED,
    EMAIL_SMTP_HOST,
    EMAIL_SMTP_PORT,
    EMAIL_SENDER,
    EMAIL_PASSWORD,
    EMAIL_RECIPIENT,
    CONSOLE_NOTIFICATIONS,
)

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def log_matches_to_console(jobs: list[dict], stats: Optional[dict] = None, limit: int = 25) -> None:
    """Print top matches to the console (no Telegram required)."""
    if not CONSOLE_NOTIFICATIONS or not jobs:
        return
    logger.info("── Console digest: top %d matches ──", min(limit, len(jobs)))
    for j in jobs[:limit]:
        logger.info(
            "  [%s] %s — %s | score=%s | resume=%s",
            j.get("source", "?"),
            j.get("company", "?"),
            (j.get("title") or "")[:72],
            j.get("match_score", 0),
            "yes" if j.get("resume_path") else "no",
        )
    if stats:
        logger.info(
            "  Pipeline: new=%s fetched=%s resumes=%s alerts=%s",
            stats.get("new_jobs"),
            stats.get("total_fetched"),
            stats.get("resumes_generated"),
            stats.get("alerts_sent"),
        )
    logger.info("── end digest ──")


# ═══════════════════════════════════════════════════════════════
# Telegram Notifications
# ═══════════════════════════════════════════════════════════════

def _format_telegram_alert(job: dict) -> str:
    """Format a job dict into a Telegram-friendly Markdown message."""
    score = job.get("match_score", 0)
    if score >= 80:
        stars = "🔥"
    elif score >= 60:
        stars = "⭐"
    else:
        stars = "📋"

    resume_status = "✅ Resume ready" if job.get("resume_path") else "⏳ Pending"

    salary_text = ""
    if job.get("salary_min"):
        salary_text = f"\n💰 ${job['salary_min']:,.0f}"
        if job.get("salary_max"):
            salary_text += f" - ${job['salary_max']:,.0f}"

    msg = (
        f"{stars} *New Job Match* ({score}/100)\n\n"
        f"*{_escape_md(job['title'])}*\n"
        f"🏢 {_escape_md(job['company'])}\n"
        f"📍 {_escape_md(job.get('location', 'N/A'))}\n"
        f"📄 {resume_status}"
        f"{salary_text}\n\n"
        f"[🔗 Apply Now]({job['link']})"
    )
    return msg


def _escape_md(text: str) -> str:
    """Escape Markdown special characters for Telegram."""
    if not text:
        return ""
    for char in ["_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"]:
        text = text.replace(char, f"\\{char}")
    return text


async def send_telegram_message(text: str, parse_mode: str = "Markdown") -> bool:
    """Send a single message via Telegram bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram credentials not configured, skipping")
        return False

    url = TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=15)
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                logger.warning(f"Telegram rate limited, waiting {retry_after}s")
                await asyncio.sleep(retry_after)
                resp = await client.post(url, json=payload, timeout=15)

            resp.raise_for_status()
            logger.info("Telegram alert sent successfully")
            return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


async def send_job_alert(job: dict) -> bool:
    """Send a formatted job alert via Telegram."""
    message = _format_telegram_alert(job)
    return await send_telegram_message(message)


async def send_batch_alerts(jobs: list[dict]) -> int:
    """Send alerts for multiple jobs. Returns count of successful sends."""
    if not jobs:
        return 0

    sent = 0
    for job in jobs:
        success = await send_job_alert(job)
        if success:
            sent += 1
        await asyncio.sleep(0.5)  # Respect Telegram rate limits

    logger.info(f"Telegram: Sent {sent}/{len(jobs)} alerts")
    return sent


async def send_summary_alert(stats: dict) -> bool:
    """Send a pipeline summary alert via Telegram."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = (
        f"📊 *Pipeline Summary* — {now}\n\n"
        f"🆕 New jobs: {stats.get('new_jobs', 0)}\n"
        f"📡 Total fetched: {stats.get('total_fetched', 0)}\n"
        f"✅ Resumes generated: {stats.get('resumes_generated', 0)}\n"
        f"🔔 Alerts sent: {stats.get('alerts_sent', 0)}\n"
        f"🏆 Top score: {stats.get('top_score', 0)}\n"
        f"📁 Total in DB: {stats.get('total_jobs', 0)}\n"
        f"⏱️ Duration: {stats.get('duration', 0):.1f}s"
    )
    if stats.get("errors"):
        msg += f"\n\n⚠️ Errors: {len(stats['errors'])}"
    return await send_telegram_message(msg)


# ═══════════════════════════════════════════════════════════════
# Email Notifications
# ═══════════════════════════════════════════════════════════════

def _build_job_html(job: dict) -> str:
    """Build an HTML card for a single job."""
    score = job.get("match_score", 0)
    color = "#10b981" if score >= 75 else "#f59e0b" if score >= 50 else "#ef4444"

    salary_html = ""
    if job.get("salary_min"):
        salary_html = f'<p style="color: #059669; font-weight: 600;">💰 ${job["salary_min"]:,.0f}'
        if job.get("salary_max"):
            salary_html += f' - ${job["salary_max"]:,.0f}'
        salary_html += "</p>"

    resume_html = "✅ Resume ready" if job.get("resume_path") else "⏳ Pending"

    return f"""
    <div style="border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px; margin-bottom: 12px; border-left: 4px solid {color};">
        <div style="display: flex; justify-content: space-between; align-items: start;">
            <div>
                <h3 style="margin: 0 0 4px 0; color: #1f2937;">{job['title']}</h3>
                <p style="margin: 0; color: #6b7280;">🏢 {job['company']} · 📍 {job.get('location', 'N/A')}</p>
                {salary_html}
            </div>
            <div style="text-align: right;">
                <span style="background: {color}; color: white; padding: 4px 10px; border-radius: 12px; font-weight: 700; font-size: 14px;">{score}</span>
                <p style="margin: 4px 0 0 0; font-size: 12px; color: #9ca3af;">{resume_html}</p>
            </div>
        </div>
        <a href="{job['link']}" style="display: inline-block; margin-top: 8px; color: #2563eb; text-decoration: none; font-weight: 500;">Apply Now →</a>
    </div>
    """


def send_email_digest(jobs: list[dict], stats: dict = None) -> bool:
    """Send an HTML email digest with job matches."""
    if not EMAIL_ENABLED:
        logger.debug("Email notifications disabled")
        return False

    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT]):
        logger.warning("Email credentials incomplete, skipping")
        return False

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Build HTML email
    jobs_html = "\n".join(_build_job_html(j) for j in jobs[:25])

    stats_html = ""
    if stats:
        stats_html = f"""
        <div style="background: #f3f4f6; border-radius: 8px; padding: 16px; margin-bottom: 20px;">
            <h3 style="margin: 0 0 8px 0;">📊 Pipeline Summary</h3>
            <p style="margin: 2px 0;">🆕 New jobs: <strong>{stats.get('new_jobs', 0)}</strong></p>
            <p style="margin: 2px 0;">✅ Resumes generated: <strong>{stats.get('resumes_generated', 0)}</strong></p>
            <p style="margin: 2px 0;">🏆 Top score: <strong>{stats.get('top_score', 0)}</strong></p>
        </div>
        """

    html_body = f"""
    <html>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #1f2937;">
        <h1 style="color: #1a1a2e; border-bottom: 2px solid #2563eb; padding-bottom: 8px;">
            🎯 Job Automation Report
        </h1>
        <p style="color: #6b7280; font-size: 14px;">{now}</p>

        {stats_html}

        <h2>Top Matches ({len(jobs)} jobs)</h2>
        {jobs_html}

        <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 20px 0;">
        <p style="font-size: 12px; color: #9ca3af; text-align: center;">
            Job Automation System • Does not auto-apply • Respects rate limits
        </p>
    </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎯 Job Report: {len(jobs)} matches — {now}"
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECIPIENT
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)

        logger.info(f"Email digest sent to {EMAIL_RECIPIENT} ({len(jobs)} jobs)")
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("Email authentication failed. Check EMAIL_SENDER and EMAIL_PASSWORD.")
        return False
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# Unified Notification API
# ═══════════════════════════════════════════════════════════════

async def notify_new_jobs(jobs: list[dict], stats: dict = None) -> dict:
    """
    Send notifications through all configured channels.
    Returns dict with results per channel.
    """
    results = {"telegram": 0, "email": False, "console": False}

    if CONSOLE_NOTIFICATIONS and jobs:
        log_matches_to_console(jobs, stats)
        results["console"] = True

    # Telegram alerts
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        results["telegram"] = await send_batch_alerts(jobs)

    # Email digest
    if EMAIL_ENABLED and jobs:
        results["email"] = send_email_digest(jobs, stats)

    # Summary alert
    if stats and (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        await send_summary_alert(stats)

    return results


# ─── Sync Wrappers ──────────────────────────────────────────────

def send_alert_sync(job: dict) -> bool:
    return asyncio.run(send_job_alert(job))


def send_batch_alerts_sync(jobs: list[dict]) -> int:
    return asyncio.run(send_batch_alerts(jobs))


def notify_sync(jobs: list[dict], stats: dict = None) -> dict:
    return asyncio.run(notify_new_jobs(jobs, stats))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_job = {
        "title": "Senior Data Engineer",
        "company": "Stripe",
        "location": "Remote",
        "link": "https://example.com/apply",
        "match_score": 85,
        "resume_path": "data/resumes/test.pdf",
        "salary_min": 180000,
        "salary_max": 250000,
    }
    success = send_alert_sync(test_job)
    print(f"Alert sent: {success}")
