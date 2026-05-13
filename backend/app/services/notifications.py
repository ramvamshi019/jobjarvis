"""
Real-time notifications for new top matches.

Two channels:
  • Slack webhook  — set SLACK_WEBHOOK_URL on the user (per-user)
  • Email          — uses the same SMTP env vars as the daily digest

Behavior:
  • Polls every 15 min (via Celery beat)
  • For each user, computes their current top-10 matches
  • Compares to the last-notified set in `notification_state`
  • If there are NEW jobs in the top-10 that weren't there before:
      → push a notification with title, company, link
      → record them so we don't re-notify

Schema:

  CREATE TABLE notification_state (
      user_id INTEGER PRIMARY KEY,
      last_notified_job_ids JSONB NOT NULL DEFAULT '[]',
      last_notified_at TIMESTAMPTZ
  );
"""
import json
import os
import smtplib
import logging
from email.message import EmailMessage
from typing import Optional

import asyncpg
import httpx

logger = logging.getLogger(__name__)


DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://jobjarvis:jobjarvis@postgres:5432/jobjarvis",
).replace("postgresql+asyncpg://", "postgresql://")


_ENSURE_TABLE = """
CREATE TABLE IF NOT EXISTS notification_state (
    user_id INTEGER PRIMARY KEY,
    last_notified_job_ids JSONB NOT NULL DEFAULT '[]',
    last_notified_at TIMESTAMPTZ
);
"""


# ── Channel: Slack ───────────────────────────────────────────────────────────

async def send_slack(webhook_url: str, payload: dict) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(webhook_url, json=payload)
            return r.status_code == 200
    except Exception as e:
        logger.warning("slack_send_failed", exc_info=True)
        return False


def slack_block_payload(user_name: str, matches: list[dict]) -> dict:
    blocks: list = [
        {"type": "header",
         "text": {"type": "plain_text",
                  "text": f"🎯 {len(matches)} new job match(es) for {user_name}"}},
        {"type": "divider"},
    ]
    for m in matches[:10]:
        pct = int(m["match_score"] * 100)
        title = m["title"]
        company = m["company_name"]
        url = m.get("apply_url") or m.get("url") or "#"
        loc = m.get("location") or "—"
        salary = ""
        if m.get("salary_min"):
            salary = f" · ${m['salary_min']//1000}k+"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*<{url}|{title}>* — {company}\n"
                        f"📍 {loc}{salary} · *{pct}% match*",
            },
        })
    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [{
        "type": "mrkdwn", "text": "via JobJarvis",
    }]})
    return {"text": f"{len(matches)} new job matches", "blocks": blocks}


# ── Channel: Email ───────────────────────────────────────────────────────────

def send_email(to_email: str, subject: str, html: str, text: str) -> bool:
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pwd  = os.environ.get("SMTP_PASS")
    sender = os.environ.get("SMTP_FROM", user)
    if not (host and user and pwd):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        return True
    except Exception:
        logger.warning("email_send_failed", exc_info=True)
        return False


def render_email(user_name: str, matches: list[dict]) -> tuple[str, str]:
    """Returns (subject, html, text)."""
    subject = f"🎯 {len(matches)} new job match{'es' if len(matches)!=1 else ''} for you"
    lines_t = [f"Hi {user_name}, you have {len(matches)} new high-match jobs:", ""]
    rows_h = []
    for m in matches[:10]:
        pct = int(m["match_score"] * 100)
        url = m.get("apply_url") or m.get("url") or "#"
        loc = m.get("location") or "—"
        salary = ""
        if m.get("salary_min"):
            salary = f" · ${m['salary_min']//1000}k+"
        lines_t.append(f"- [{pct}%] {m['title']} @ {m['company_name']} ({loc}){salary}")
        lines_t.append(f"   {url}")
        lines_t.append("")
        rows_h.append(f"""
<tr><td style="padding:12px 0;border-bottom:1px solid #eee">
  <div style="font-size:13px;font-weight:600">{m['title']}</div>
  <div style="font-size:12px;color:#1d4ed8">{m['company_name']}</div>
  <div style="font-size:11px;color:#888;margin-top:4px">📍 {loc}{salary} · <strong>{pct}% match</strong></div>
  <a href="{url}" style="display:inline-block;margin-top:8px;padding:6px 12px;
       background:#1d4ed8;color:#fff;text-decoration:none;border-radius:6px;font-size:12px">Apply →</a>
</td></tr>""")
    html = f"""<!doctype html><html><body style="font-family:-apple-system,sans-serif">
<table cellpadding="0" cellspacing="0" style="max-width:560px;margin:32px auto;
       background:#fff;border-radius:12px;padding:20px">
<tr><td><h2 style="margin:0 0 8px 0">JobJarvis</h2>
<p style="margin:0 0 16px 0;color:#666">
{len(matches)} new high-match job{"s" if len(matches)!=1 else ""} for you</p></td></tr>
<tr><td><table cellpadding="0" cellspacing="0" width="100%">{''.join(rows_h)}</table></td></tr>
</table></body></html>"""
    return subject, html, "\n".join(lines_t)


# ── Orchestrator ─────────────────────────────────────────────────────────────

async def notify_new_matches_for_user(
    conn: asyncpg.Connection, user_id: int, top: int = 10,
) -> dict:
    """
    Compare user's current top-10 matches to the last set we notified about.
    Push notifications for jobs that are NEW.
    """
    await conn.execute(_ENSURE_TABLE)

    # Load user
    user = await conn.fetchrow(
        "SELECT email, full_name, COALESCE(linkedin_url, '') AS linkedin, "
        "       notify_email FROM users WHERE id=$1", user_id,
    )
    if not user:
        return {"error": "user not found"}

    # Load slack webhook (we store it in user table extension if present)
    # For now: read from env var per-user is too much; use a single SLACK_WEBHOOK_URL
    slack_url = os.environ.get("SLACK_WEBHOOK_URL", "")

    # Pull top matches that pass the US+Remote filter
    rows = await conn.fetch(
        """
        SELECT jm.match_score, jm.sim_score,
               j.id AS job_id, j.title, j.company_name, j.location,
               j.salary_min, j.salary_max, j.url, j.apply_url,
               c.ats AS source_ats
        FROM job_matches jm
        JOIN jobs j      ON j.id  = jm.job_id
        JOIN companies c ON c.id  = j.company_id
        WHERE jm.user_id = $1
          AND j.active = true
          AND c.active = true
          AND (j.country IN ('US','REMOTE') OR j.country IS NULL)
        ORDER BY jm.match_score DESC
        LIMIT $2
        """,
        user_id, top,
    )
    if not rows:
        return {"new_matches": 0, "reason": "no_matches"}

    current_ids = [r["job_id"] for r in rows]
    current_matches = [dict(r) for r in rows]

    # Load last-notified set
    state = await conn.fetchrow(
        "SELECT last_notified_job_ids FROM notification_state WHERE user_id=$1",
        user_id,
    )
    last_ids = set(json.loads(state["last_notified_job_ids"])) if state else set()
    new_ids = [jid for jid in current_ids if jid not in last_ids]
    if not new_ids:
        return {"new_matches": 0, "reason": "no_new"}

    new_matches = [m for m in current_matches if m["job_id"] in new_ids]

    # Send notifications
    sent = {"slack": False, "email": False}
    if slack_url:
        sent["slack"] = await send_slack(slack_url,
                                          slack_block_payload(user["full_name"] or user["email"],
                                                              new_matches))
    if user["email"] and user["notify_email"]:
        subject, html, text = render_email(user["full_name"] or user["email"].split("@")[0],
                                            new_matches)
        sent["email"] = send_email(user["email"], subject, html, text)

    # Persist the new top-10 set
    await conn.execute(
        """
        INSERT INTO notification_state (user_id, last_notified_job_ids, last_notified_at)
        VALUES ($1, $2::jsonb, NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            last_notified_job_ids = EXCLUDED.last_notified_job_ids,
            last_notified_at = NOW()
        """,
        user_id, json.dumps(current_ids),
    )

    return {"new_matches": len(new_matches), "sent": sent, "ids": new_ids}


async def notify_all_users() -> dict:
    """Entry-point called by Celery beat."""
    conn = await asyncpg.connect(DB_DSN)
    try:
        await conn.execute(_ENSURE_TABLE)
        users = await conn.fetch(
            "SELECT id FROM users WHERE is_active=true"
        )
        total_new = 0
        per_user = []
        for u in users:
            res = await notify_new_matches_for_user(conn, u["id"])
            if res.get("new_matches", 0) > 0:
                total_new += res["new_matches"]
            per_user.append({"user_id": u["id"], **res})
        return {"users": len(users), "total_new_matches": total_new,
                "per_user": per_user}
    finally:
        await conn.close()
