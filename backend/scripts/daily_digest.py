"""
Daily personalized job digest — the recurring touchpoint that retains users.

Reads from job_matches (populated by ai_match_jobs.py) and renders an HTML
email + plain-text digest of the user's top matches today.

Outputs to disk by default; can be wired to SMTP / SendGrid / Resend later.

Usage:
  docker exec jobjarvis_celery_worker python3 -u /tmp/daily_digest.py \\
      --user 1
  docker exec jobjarvis_celery_worker python3 -u /tmp/daily_digest.py \\
      --user 1 --send  # actually send via SMTP env vars

Environment vars for sending:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM
"""
import argparse
import asyncio
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import asyncpg

DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://jobjarvis:jobjarvis@postgres:5432/jobjarvis",
).replace("postgresql+asyncpg://", "postgresql://")

OUTPUT_DIR = Path(os.environ.get("DIGEST_DIR", "/tmp/digests"))


# ── Data fetch ───────────────────────────────────────────────────────────────

DIGEST_SQL = """
SELECT
    jm.match_score,
    jm.sim_score,
    j.id, j.title, j.company_name, j.location, j.remote_type,
    j.salary_min, j.salary_max, j.url, j.apply_url,
    j.description, j.posted_at, j.first_seen_at,
    c.name AS company, c.ats AS source_ats
FROM job_matches jm
JOIN jobs j ON j.id = jm.job_id
JOIN companies c ON c.id = j.company_id
WHERE jm.user_id = $1
  AND j.active = true
  AND c.active = true
ORDER BY jm.match_score DESC
LIMIT $2;
"""


async def fetch_user(conn, user_id: int) -> dict | None:
    row = await conn.fetchrow(
        "SELECT id, email, full_name FROM users WHERE id = $1", user_id
    )
    return dict(row) if row else None


async def fetch_matches(conn, user_id: int, top: int) -> list[dict]:
    rows = await conn.fetch(DIGEST_SQL, user_id, top)
    return [dict(r) for r in rows]


# ── Rendering ────────────────────────────────────────────────────────────────

def fmt_salary(lo, hi):
    if not lo and not hi:
        return "—"
    if lo and hi:
        return f"${lo//1000}k–${hi//1000}k"
    if lo:
        return f"${lo//1000}k+"
    return f"up to ${hi//1000}k"


def fmt_age(posted_at, first_seen_at):
    ts = posted_at or first_seen_at
    if not ts:
        return ""
    days = (datetime.now(timezone.utc) - ts).days
    if days == 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


def render_text(user: dict, matches: list[dict]) -> str:
    name = (user.get("full_name") or "there").split()[0]
    today = datetime.now(timezone.utc).strftime("%a %b %-d")
    lines = [
        f"Hey {name}, here are your {len(matches)} top job matches for {today}.",
        "",
    ]
    for i, m in enumerate(matches, 1):
        lines.append(
            f"{i}. {m['title']} @ {m['company']}"
        )
        meta = [
            m.get("location") or "—",
            m.get("remote_type") or "",
            fmt_salary(m.get("salary_min"), m.get("salary_max")),
            fmt_age(m.get("posted_at"), m.get("first_seen_at")),
        ]
        lines.append("   " + " · ".join(p for p in meta if p))
        lines.append(f"   match: {m['match_score']*100:.0f}%   "
                     f"source: {m['source_ats']}")
        lines.append(f"   apply: {m['apply_url'] or m['url']}")
        lines.append("")
    return "\n".join(lines)


def render_html(user: dict, matches: list[dict]) -> str:
    name = (user.get("full_name") or "there").split()[0]
    today = datetime.now(timezone.utc).strftime("%a %b %-d")
    rows = []
    for i, m in enumerate(matches, 1):
        rows.append(f"""
<tr style="border-bottom:1px solid #eee">
  <td style="padding:14px 8px;font-size:14px;color:#666;width:32px">{i}</td>
  <td style="padding:14px 8px">
    <div style="font-size:16px;font-weight:600;color:#111">{esc(m['title'])}</div>
    <div style="font-size:14px;color:#444;margin-top:2px">{esc(m['company'])}</div>
    <div style="font-size:12px;color:#888;margin-top:6px">
      {esc(m.get('location') or '—')} · {esc(m.get('remote_type') or '')}
      · {esc(fmt_salary(m.get('salary_min'), m.get('salary_max')))}
      · {esc(fmt_age(m.get('posted_at'), m.get('first_seen_at')))}
    </div>
    <div style="margin-top:8px">
      <span style="display:inline-block;background:#e0f2fe;color:#075985;
                   padding:2px 8px;border-radius:10px;font-size:11px;
                   font-weight:600">{int(m['match_score']*100)}% match</span>
      <span style="display:inline-block;background:#f3f4f6;color:#666;
                   padding:2px 8px;border-radius:10px;font-size:11px;
                   margin-left:6px">{esc(m['source_ats'])}</span>
    </div>
  </td>
  <td style="padding:14px 8px;text-align:right">
    <a href="{esc(m['apply_url'] or m['url'])}"
       style="display:inline-block;background:#111;color:#fff;
              padding:8px 14px;border-radius:6px;font-size:13px;
              font-weight:600;text-decoration:none">Apply</a>
  </td>
</tr>""")

    return f"""<!doctype html>
<html><body style="margin:0;background:#f7f7f8;font-family:-apple-system,system-ui,sans-serif">
<table cellpadding="0" cellspacing="0" border="0" align="center"
       style="width:600px;max-width:100%;background:#fff;margin:32px auto;
              border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.06)">
  <tr><td style="padding:28px 32px 12px 32px">
    <div style="font-size:24px;font-weight:700;color:#111">JobJarvis</div>
    <div style="font-size:14px;color:#666;margin-top:4px">
      {today} · {len(matches)} top matches for {esc(name)}
    </div>
  </td></tr>
  <tr><td style="padding:0 32px 24px 32px">
    <table cellpadding="0" cellspacing="0" border="0" width="100%"
           style="border-top:1px solid #eee">
      {''.join(rows)}
    </table>
  </td></tr>
  <tr><td style="padding:16px 32px;background:#fafafa;font-size:11px;
                  color:#999;text-align:center">
    Personalized by AI matching. Filter or unsubscribe in JobJarvis.
  </td></tr>
</table>
</body></html>"""


def esc(s):
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ── Sending ──────────────────────────────────────────────────────────────────

def send_email(to_email: str, html: str, text: str, subject: str):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pwd = os.environ.get("SMTP_PASS")
    sender = os.environ.get("SMTP_FROM", user)
    if not host or not user or not pwd:
        print("  SMTP env vars missing — skipping send", flush=True)
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
    except Exception as e:
        print(f"  send error: {e}", flush=True)
        return False


# ── Main ─────────────────────────────────────────────────────────────────────

async def run_for_user(conn, user_id: int, top: int, send: bool, save: bool):
    user = await fetch_user(conn, user_id)
    if not user:
        print(f"  user {user_id} not found", flush=True)
        return

    matches = await fetch_matches(conn, user_id, top)
    if not matches:
        print(f"  user {user_id}: no matches (run ai_match_jobs.py first)",
              flush=True)
        return

    text = render_text(user, matches)
    html = render_html(user, matches)
    subject = f"Your {len(matches)} job matches for {datetime.now().strftime('%a %b %-d')}"

    if save:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        (OUTPUT_DIR / f"digest_user{user_id}_{ts}.html").write_text(html)
        (OUTPUT_DIR / f"digest_user{user_id}_{ts}.txt").write_text(text)
        print(f"  user {user_id}: saved digest "
              f"({len(matches)} matches) → {OUTPUT_DIR}/", flush=True)

    if send and user.get("email"):
        ok = send_email(user["email"], html, text, subject)
        print(f"  user {user_id}: sent email to {user['email']}: "
              f"{'OK' if ok else 'FAILED'}", flush=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", type=int, default=None)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--send", action="store_true",
                        help="Send via SMTP (requires SMTP_* env vars)")
    parser.add_argument("--no-save", action="store_true",
                        help="Don't save HTML/text to disk")
    args = parser.parse_args()

    print(f"Connecting to {DB_DSN.split('@')[-1]}…", flush=True)
    conn = await asyncpg.connect(DB_DSN)

    if args.user:
        users = [args.user]
    else:
        rows = await conn.fetch("SELECT id FROM users WHERE is_active=true")
        users = [r["id"] for r in rows]

    print(f"Generating digests for {len(users)} user(s)\n", flush=True)
    for uid in users:
        await run_for_user(conn, uid, args.top, args.send, not args.no_save)

    await conn.close()
    if not args.no_save:
        print(f"\nDigests saved to {OUTPUT_DIR}/", flush=True)
        print("To preview the HTML, copy it out:", flush=True)
        print(f"  docker cp jobjarvis_celery_worker:{OUTPUT_DIR}/ ./digests/",
              flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
