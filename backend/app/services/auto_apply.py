"""
Auto-apply agent — fills application forms on the user's behalf.

Supports the two most common ATSes (covers ~70% of your indexed jobs):
  • Greenhouse
  • Lever

Safety:
  • Default mode is dry_run=True (fills but does NOT submit, takes screenshot)
  • Real submission only when dry_run=False
  • Captures a screenshot of every page for audit trail

Storage:
  • Each run logs a row to the `auto_apply_runs` table (created on first use)
  • Screenshots saved under /tmp/auto_apply/{user_id}/{job_id}.png
"""
import asyncio
import os
import re
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import asyncpg

logger = logging.getLogger(__name__)


DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://jobjarvis:jobjarvis@postgres:5432/jobjarvis",
).replace("postgresql+asyncpg://", "postgresql://")


# ── Profile data ─────────────────────────────────────────────────────────────

class ApplicantProfile:
    """Fields that we'll try to fill on every form."""
    def __init__(
        self,
        full_name: str,
        email: str,
        phone: str = "",
        linkedin: str = "",
        github: str = "",
        portfolio: str = "",
        location: str = "",
        work_authorization: str = "Yes",
        requires_sponsorship: str = "No",
        years_of_experience: str = "",
    ):
        self.full_name = full_name
        self.first_name = full_name.split()[0] if full_name else ""
        self.last_name  = " ".join(full_name.split()[1:]) if full_name else ""
        self.email = email
        self.phone = phone
        self.linkedin = linkedin
        self.github = github
        self.portfolio = portfolio
        self.location = location
        self.work_authorization = work_authorization
        self.requires_sponsorship = requires_sponsorship
        self.years_of_experience = years_of_experience


# ── ATS detection ────────────────────────────────────────────────────────────

def detect_ats(url: str) -> str | None:
    host = urlparse(url).hostname or ""
    if "greenhouse.io" in host: return "greenhouse"
    if "lever.co" in host:      return "lever"
    if "ashbyhq.com" in host:   return "ashby"
    return None


# ── Logging table ────────────────────────────────────────────────────────────

_ENSURE_TABLE = """
CREATE TABLE IF NOT EXISTS auto_apply_runs (
    id           BIGSERIAL PRIMARY KEY,
    user_id      INTEGER     NOT NULL,
    job_id       BIGINT      NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    dry_run      BOOLEAN     NOT NULL,
    success      BOOLEAN     NOT NULL DEFAULT false,
    ats          VARCHAR(50),
    screenshot   TEXT,
    error        TEXT,
    fields_filled INTEGER DEFAULT 0,
    drafts_json  JSONB,
    status       VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_aar_user_job ON auto_apply_runs(user_id, job_id);
CREATE INDEX IF NOT EXISTS ix_aar_status   ON auto_apply_runs(user_id, status);
"""


async def _ensure_table(conn):
    await conn.execute(_ENSURE_TABLE)


async def _log_run(*, user_id, job_id, dry_run, success, ats=None,
                   screenshot=None, error=None, fields_filled=0,
                   drafts=None):
    import json as _json
    try:
        conn = await asyncpg.connect(DB_DSN)
        await _ensure_table(conn)
        await conn.execute(
            """
            INSERT INTO auto_apply_runs
              (user_id, job_id, dry_run, success, ats, screenshot, error,
               fields_filled, drafts_json, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
            """,
            user_id, job_id, dry_run, success, ats, screenshot, error,
            fields_filled,
            _json.dumps(drafts or []),
            "pending" if (dry_run and success) else ("submitted" if success else "failed"),
        )
        await conn.close()
    except Exception:
        logger.warning("auto_apply.log_failed", exc_info=True)


# ── Form fillers (Playwright) ────────────────────────────────────────────────

async def fill_greenhouse_form(page, profile, resume_path: str) -> int:
    filled = 0
    field_mappings = [
        ("first_name",  profile.first_name),
        ("last_name",   profile.last_name),
        ("email",       profile.email),
        ("phone",       profile.phone),
    ]
    for field_name, value in field_mappings:
        if not value: continue
        try:
            el = page.locator(
                f"input[name*='{field_name}'], input[id*='{field_name}']"
            ).first
            if await el.count() > 0:
                await el.fill(value)
                filled += 1
        except Exception:
            continue

    for label, value in [
        ("linkedin",  profile.linkedin),
        ("github",    profile.github),
        ("portfolio", profile.portfolio),
        ("website",   profile.portfolio),
    ]:
        if not value: continue
        try:
            el = page.locator(
                f"input[name*='{label}' i], textarea[name*='{label}' i]"
            ).first
            if await el.count() > 0:
                await el.fill(value)
                filled += 1
        except Exception:
            continue

    if resume_path and Path(resume_path).exists():
        try:
            file_input = page.locator("input[type='file']").first
            if await file_input.count() > 0:
                await file_input.set_input_files(resume_path)
                filled += 1
        except Exception:
            pass

    try:
        select_el = page.locator(
            "select[name*='authoriz' i], select[name*='visa' i]"
        ).first
        if await select_el.count() > 0:
            await select_el.select_option(label=profile.work_authorization)
            filled += 1
    except Exception:
        pass

    return filled


async def fill_lever_form(page, profile, resume_path: str) -> int:
    filled = 0

    # Standard text/email/url inputs Lever exposes.  For each logical field we
    # try several name variants because Lever's markup is not perfectly
    # consistent across customers / form versions.
    field_mappings = [
        # logical-name              value                     candidate names
        ("name",                    profile.full_name,        ["name", "full_name", "fullName"]),
        ("email",                   profile.email,            ["email", "applicant.email"]),
        ("phone",                   profile.phone,            ["phone", "applicant.phone"]),
        ("location",                profile.location,         ["location", "current_location", "currentLocation"]),
        ("org",                     "",                       ["org", "company", "current_company"]),
        ("linkedin",                profile.linkedin,         ["urls[LinkedIn]", "linkedin", "linkedinUrl"]),
        ("github",                  profile.github,           ["urls[GitHub]", "github", "githubUrl"]),
        ("portfolio",               profile.portfolio,        ["urls[Portfolio]", "portfolio", "website"]),
    ]
    for _logical, value, candidates in field_mappings:
        if not value:
            continue
        for nm in candidates:
            try:
                el = page.locator(
                    f"input[name='{nm}'], textarea[name='{nm}']"
                ).first
                if await el.count() > 0:
                    await el.fill(value)
                    filled += 1
                    break
            except Exception:
                continue

    # Preferred name (first name) — fuzzy match because the field name varies
    if profile.first_name:
        try:
            el = page.locator(
                "input[name*='preferred' i], input[placeholder*='preferred' i]"
            ).first
            if await el.count() > 0 and not (await el.input_value() or "").strip():
                await el.fill(profile.first_name)
                filled += 1
        except Exception:
            pass

    # Resume file — first file input on Lever is always resume
    if resume_path and Path(resume_path).exists():
        try:
            file_input = page.locator("input[type='file']").first
            if await file_input.count() > 0:
                await file_input.set_input_files(resume_path)
                filled += 1
        except Exception:
            pass

    # ── Radio buttons: do everything inside one page.evaluate() so we can
    # dispatch React-compatible `input` and `change` events directly.
    # Playwright's .check() on a hidden Lever radio doesn't always trigger
    # React's state update, so we set .checked manually AND fire events AND
    # click the label as a belt-and-suspenders fallback.
    try:
        click_result = await page.evaluate(
            """
            () => {
                const result = {clicked: 0, groups: []};
                const groups = {};
                document.querySelectorAll("input[type='radio']").forEach(r => {
                    if (!r.name) return;
                    if (!groups[r.name]) groups[r.name] = [];
                    groups[r.name].push(r);
                });
                const PATTERNS = [
                    [/non.?compete|non.?solicit|post.?employment restriction/, 'no'],
                    [/require sponsorship|visa sponsorship|future sponsorship|h-1b|h1b|sponsorship for employment/, 'no'],
                    [/authorized to work|legally authorized|eligible to work|work eligibility/, 'yes'],
                    [/comfortable with the salary|reviewed and are comfortable|salary range listed/, 'yes'],
                    [/agree to provide|i agree/, 'i agree'],
                ];
                for (const [name, radios] of Object.entries(groups)) {
                    // Walk up from the first radio to find the question text.
                    let qtext = '';
                    let cur = radios[0];
                    for (let i = 0; i < 8; i++) {
                        cur = cur.parentElement;
                        if (!cur) break;
                        const clone = cur.cloneNode(true);
                        clone.querySelectorAll('input, label').forEach(e => e.remove());
                        const t = (clone.innerText || clone.textContent || '').trim();
                        if (t && t.length > 15 && t.length < 800) {
                            qtext = t.toLowerCase();
                            break;
                        }
                    }
                    let target = null;
                    for (const [re, tgt] of PATTERNS) {
                        if (re.test(qtext)) { target = tgt; break; }
                    }
                    result.groups.push({name, qtext: qtext.slice(0, 80), target});
                    if (!target) continue;

                    // Find the matching radio in this group.
                    let chosen = null;
                    for (const r of radios) {
                        const val = (r.value || '').trim().toLowerCase();
                        let lt = '';
                        if (r.id) {
                            const l = document.querySelector(`label[for='${r.id}']`);
                            if (l) lt = (l.innerText || '').trim().toLowerCase();
                        }
                        if (!lt) {
                            const w = r.closest('label');
                            if (w) lt = (w.innerText || '').trim().toLowerCase();
                        }
                        if (val === target || lt === target || lt.startsWith(target + ' ') || lt === target) {
                            chosen = r;
                            break;
                        }
                    }
                    if (!chosen) continue;

                    // Three-way click for React compatibility:
                    //   1. Set .checked via the native setter (so React picks it up)
                    //   2. Dispatch input + change events
                    //   3. Click the associated label
                    try {
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'checked'
                        ).set;
                        setter.call(chosen, true);
                        chosen.dispatchEvent(new Event('input', {bubbles: true}));
                        chosen.dispatchEvent(new Event('change', {bubbles: true}));
                        const lbl = chosen.id
                            ? document.querySelector(`label[for='${chosen.id}']`)
                            : chosen.closest('label');
                        if (lbl) lbl.click();
                        result.clicked++;
                    } catch (e) { /* ignore */ }
                }
                return result;
            }
            """
        )
        filled += int((click_result or {}).get("clicked", 0) or 0)
    except Exception:
        pass

    # ── EEOC / demographic <select> dropdowns: pick "Decline to answer".
    # These are usually Gender / Race / Veteran status / Disability.
    try:
        selects = page.locator("select")
        n_sel = await selects.count()
        for i in range(n_sel):
            try:
                sel = selects.nth(i)
                # Skip selects that already have a non-empty / non-default value.
                current = (await sel.input_value()) or ""
                if current and current.strip() not in {"", "Select...", "-"}:
                    continue

                # Get the surrounding text to infer the question.
                container = sel.locator(
                    "xpath=ancestor::*[self::div or self::fieldset or self::label][1]"
                ).first
                ctext = ""
                try:
                    if await container.count() > 0:
                        ctext = ((await container.inner_text()) or "").lower()
                except Exception:
                    pass

                is_demo = any(k in ctext for k in
                              ["gender", "race", "ethnic", "veteran",
                               "disability", "lgbtq", "pronoun"])
                # Also consider any select whose options literally include "decline".
                options = sel.locator("option")
                n_opt = await options.count()
                option_texts: list[str] = []
                for j in range(n_opt):
                    try:
                        option_texts.append(((await options.nth(j).inner_text()) or "").strip())
                    except Exception:
                        continue
                joined = " | ".join(option_texts).lower()

                if not is_demo and "decline" not in joined:
                    continue

                # Pick the first option whose text mentions "decline"
                pick = None
                for txt in option_texts:
                    if "decline" in txt.lower():
                        pick = txt
                        break
                if not pick:
                    continue
                try:
                    await sel.select_option(label=pick, timeout=1500)
                    filled += 1
                except Exception:
                    pass
            except Exception:
                continue
    except Exception:
        pass

    return filled


# ── Custom-question AI filler ───────────────────────────────────────────────

# Field names we've already handled — skip them when scanning for "extra" textareas
_KNOWN_FIELD_TOKENS = {
    "name", "email", "phone", "first_name", "last_name", "location", "org",
    "urls", "linkedin", "github", "portfolio", "website", "twitter",
    "resume", "cv", "cover", "cover_letter",
}


async def _find_question_label(elem) -> str:
    """
    Find the human-readable question text that belongs to a form element.
    Uses page.evaluate() with a DOM walker — XPath wasn't reliable across
    Lever/Greenhouse/Ashby's varied markup.
    """
    try:
        text = await elem.evaluate(
            """
            (el) => {
                // 1. aria-label / aria-labelledby
                const aria = el.getAttribute('aria-label');
                if (aria && aria.length > 8) return aria.trim();
                const labelledBy = el.getAttribute('aria-labelledby');
                if (labelledBy) {
                    const ref = document.getElementById(labelledBy);
                    if (ref) {
                        const t = (ref.innerText || '').trim();
                        if (t.length > 8) return t;
                    }
                }
                // 2. <label for="el.id">
                if (el.id) {
                    const l = document.querySelector(`label[for='${el.id}']`);
                    if (l) {
                        const t = (l.innerText || '').trim();
                        if (t.length > 8) return t;
                    }
                }
                // 3. Closest <label> ancestor
                const wrap = el.closest('label');
                if (wrap) {
                    const clone = wrap.cloneNode(true);
                    clone.querySelectorAll('input, textarea, select').forEach(e => e.remove());
                    const t = (clone.innerText || '').trim();
                    if (t.length > 8) return t;
                }
                // 4. Walk up looking for sibling text containers above us
                let cur = el;
                for (let depth = 0; depth < 6; depth++) {
                    cur = cur.parentElement;
                    if (!cur) break;
                    // (a) previous element siblings of `cur`
                    let sib = cur.previousElementSibling;
                    while (sib) {
                        const t = (sib.innerText || '').trim();
                        if (t && t.length > 12 && t.length < 800) return t;
                        sib = sib.previousElementSibling;
                    }
                    // (b) clone of cur minus form controls — captures the
                    //     question prompt that wraps the textarea
                    const clone = cur.cloneNode(true);
                    clone.querySelectorAll('input, textarea, select, button').forEach(e => e.remove());
                    const t = (clone.innerText || '').trim();
                    if (t && t.length > 12 && t.length < 800) return t;
                }
                return '';
            }
            """
        )
        if text and isinstance(text, str):
            text = text.strip()
            if 8 <= len(text) <= 1000:
                return text
    except Exception:
        pass
    return ""


def _is_bad_ai_answer(text: str) -> bool:
    """
    Detect when Claude responded with metacognitive filler instead of a real
    answer — happens when we pass it a junk / empty question.  We don't want
    that text in form fields.
    """
    if not text:
        return True
    t = text.lower()
    bad_phrases = [
        "please share the question",
        "please share the job description",
        "please paste the question",
        "share the application question",
        "once you share",
        "once you provide",
        "i'll craft a",
        "i'll respond right away",
        "i'll provide the answer right away",
    ]
    return any(p in t for p in bad_phrases)


def _looks_like_real_question(text: str) -> bool:
    """
    A heuristic check that this is an actual question worth answering, not
    a stray UI label like "Your response" or a textarea's internal name.
    """
    if not text:
        return False
    t = text.strip()
    if len(t) < 12:
        return False
    # Generic placeholder strings we should reject.
    generic = {
        "your response", "type here", "type your answer",
        "additional information", "comments", "notes",
        "answer", "response",
    }
    if t.lower() in generic:
        return False
    # A real question usually has a ? or is a complete sentence (>= 4 words).
    if "?" in t:
        return True
    return len(t.split()) >= 4


async def fill_custom_questions(
    page, *, resume_text: str, job_title: str, company_name: str,
    job_description: str = "",
) -> tuple[int, list[dict]]:
    """
    Scan the page for textareas + select non-standard inputs that look like
    custom essay questions.  For each, find the label, ask the AI for an
    answer, fill it in.  Returns (count_filled, [{question, answer}, ...]).
    """
    from app.services.ai_writer import answer_application_question

    filled = 0
    drafts: list[dict] = []

    # ── Phase 1: every visible <textarea> on the page ────────────────────
    try:
        textareas = page.locator("textarea")
        n = await textareas.count()

        for i in range(min(n, 20)):  # cap at 20 per form
            try:
                ta = textareas.nth(i)
                name = ((await ta.get_attribute("name")) or "").lower()
                if any(tok in name for tok in _KNOWN_FIELD_TOKENS):
                    continue
                if not await ta.is_visible():
                    continue

                # Skip if already has value
                existing = (await ta.input_value()) or ""
                if existing.strip():
                    continue

                question = await _find_question_label(ta)
                if not question:
                    question = (await ta.get_attribute("placeholder")) or ""
                # Bail out if we couldn't find a real question.  Filling these
                # with AI output produces metacognitive filler (Claude asks for
                # the question back).  Better to leave the field empty.
                if not _looks_like_real_question(question):
                    continue

                # Generate answer
                try:
                    answer = answer_application_question(
                        question=question,
                        resume_text=resume_text,
                        job_title=job_title,
                        company_name=company_name,
                        job_description=job_description,
                    )
                except Exception as e:
                    logger.warning(f"auto_apply.answer_failed q={question[:40]!r} err={e}")
                    answer = ""

                if not answer or "[No AI key configured" in answer:
                    drafts.append({"question": question, "answer": ""})
                    continue
                if _is_bad_ai_answer(answer):
                    # AI returned guidance / meta-response.  Don't fill the
                    # field — record the question so the user can answer in
                    # /review, but leave the form blank.
                    drafts.append({"question": question, "answer": ""})
                    continue

                try:
                    await ta.fill(answer)
                    filled += 1
                    drafts.append({"question": question, "answer": answer})
                except Exception as e:
                    logger.warning(f"auto_apply.fill_failed q={question[:40]!r} err={e}")
                    drafts.append({"question": question, "answer": answer})
            except Exception:
                continue
    except Exception:
        pass

    # ── Phase 2: short-answer <input type="text"> fields that aren't a
    # standard field (e.g. "How did you hear about us?", mailing address).
    try:
        inputs = page.locator("input[type='text']")
        n_in = await inputs.count()

        for i in range(min(n_in, 30)):
            try:
                inp = inputs.nth(i)
                name = ((await inp.get_attribute("name")) or "").lower()
                if any(tok in name for tok in _KNOWN_FIELD_TOKENS):
                    continue
                if not await inp.is_visible():
                    continue

                existing = (await inp.input_value()) or ""
                if existing.strip():
                    continue

                question = await _find_question_label(inp)
                if not question:
                    question = (await inp.get_attribute("placeholder")) or ""
                if not _looks_like_real_question(question):
                    continue

                # Only fill if a canned answer matches — we don't want to
                # auto-generate long AI prose into a short text input.
                from app.services.ai_writer import _try_canonical_answer
                canon = _try_canonical_answer(question)
                if not canon:
                    continue

                try:
                    await inp.fill(canon)
                    filled += 1
                    drafts.append({"question": question, "answer": canon})
                except Exception:
                    pass
            except Exception:
                continue
    except Exception:
        pass

    return filled, drafts


# ── Main entrypoint ──────────────────────────────────────────────────────────

async def apply_to_job(
    *,
    user_id: int,
    user_email: str,
    user_name: str,
    resume_path: str,
    resume_text: str,
    job_id: int,
    job_url: str,
    company: str,
    title: str,
    dry_run: bool = True,
    profile: dict | None = None,
) -> dict:
    ats = detect_ats(job_url)
    if not ats:
        await _log_run(user_id=user_id, job_id=job_id, dry_run=dry_run,
                       success=False, error=f"unsupported ATS: {job_url}")
        return {"success": False, "error": f"Unsupported ATS for {job_url}"}

    profile_dict = profile or {}
    profile = ApplicantProfile(
        full_name=profile_dict.get("full_name") or user_name,
        email=profile_dict.get("email") or user_email,
        phone=profile_dict.get("phone", ""),
        linkedin=profile_dict.get("linkedin", ""),
        github=profile_dict.get("github", ""),
        portfolio=profile_dict.get("portfolio", ""),
        location=profile_dict.get("location", ""),
        work_authorization=profile_dict.get("work_authorization", "Yes"),
    )

    out_dir = Path(f"/tmp/auto_apply/{user_id}")
    out_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = out_dir / f"{job_id}.png"

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        msg = "playwright not installed in worker container"
        await _log_run(user_id=user_id, job_id=job_id, dry_run=dry_run,
                       success=False, ats=ats, error=msg)
        return {"success": False, "error": msg}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context()
            page = await ctx.new_page()
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)

            # Click "Apply" if present
            try:
                apply_btn = page.locator("a:has-text('Apply')").first
                if await apply_btn.count() > 0:
                    await apply_btn.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass

            if   ats == "greenhouse": filled = await fill_greenhouse_form(page, profile, resume_path)
            elif ats == "lever":      filled = await fill_lever_form(page, profile, resume_path)
            else:                     filled = 0

            # AI-answer the custom essay/textarea questions.
            ai_filled, ai_drafts = await fill_custom_questions(
                page,
                resume_text=resume_text or "",
                job_title=title,
                company_name=company,
            )
            filled += ai_filled

            await page.screenshot(path=str(screenshot_path), full_page=True)

            if not dry_run:
                try:
                    submit = page.locator(
                        "button[type='submit'], input[type='submit']"
                    ).first
                    if await submit.count() > 0:
                        await submit.click()
                        await page.wait_for_load_state("networkidle", timeout=30000)
                        confirm_path = out_dir / f"{job_id}_confirm.png"
                        await page.screenshot(path=str(confirm_path), full_page=True)
                except Exception as e:
                    logger.warning(f"auto_apply.submit_failed: {e}")

            await browser.close()

            await _log_run(
                user_id=user_id, job_id=job_id, dry_run=dry_run,
                success=True, ats=ats,
                screenshot=str(screenshot_path), fields_filled=filled,
                drafts=ai_drafts,
            )
            return {
                "success": True, "ats": ats,
                "fields_filled": filled,
                "drafts": ai_drafts,
                "screenshot": str(screenshot_path),
            }
    except Exception as e:
        err = str(e)
        await _log_run(user_id=user_id, job_id=job_id, dry_run=dry_run,
                       success=False, ats=ats, error=err)
        return {"success": False, "ats": ats, "error": err}


# ── Public wrapper ───────────────────────────────────────────────────────────

async def queue_auto_apply(**kwargs):
    try:
        return await apply_to_job(**kwargs)
    except Exception:
        logger.exception("auto_apply.uncaught_error")
        return {"success": False, "error": "uncaught error"}
