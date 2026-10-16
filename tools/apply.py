"""
apply_job   — automate a single application via Playwright.
bulk_apply  — iterate over a job list with delays, skip duplicates.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any

from playwright.async_api import Page, async_playwright

from pathlib import Path

from config import APP_DIR, AppConfig, get_user_agent, load_config
from tools.session import (
    has_session,
    launch_browser,
    launch_persistent_context,
    load_cookies,
    save_cookies_from_context,
)
from tools.tracker import (
    count_recent_applications_for_company,
    is_already_applied,
    record_application,
)

BROWSER_PROFILES_DIR = APP_DIR / "browser-profiles"

logger = logging.getLogger(__name__)


def _has_saved_login(platform: str) -> bool:
    """Return True only when applying can start from a saved user login."""
    platform = platform.lower().strip()
    if platform == "linkedin":
        profile_dir = BROWSER_PROFILES_DIR / "linkedin"
        return profile_dir.is_dir() and any(profile_dir.iterdir())
    return has_session(platform)


def _missing_login_error(platform: str) -> str:
    if platform == "linkedin":
        return (
            "No saved LinkedIn login profile found. Run save_session/interactive_login "
            "for linkedin first, then retry. Real applications are blocked until "
            "the browser is logged in to your account."
        )
    return (
        f"No saved login session for {platform}. Run save_session/interactive_login "
        f"for {platform} first, then retry. Real applications are blocked so they "
        "cannot be submitted as guest/public forms."
    )


async def _looks_logged_out(page: Page, platform: str) -> bool:
    """Best-effort guard against expired sessions before submitting."""
    url = page.url.lower()
    if platform == "linkedin":
        if "/login" in url or "checkpoint" in url:
            return True
    elif platform == "indeed":
        if "secure.indeed.com/auth" in url or "/account/login" in url:
            return True
    elif platform == "naukri":
        if "/mnjuser/login" in url:
            return True
        login_apply = await page.query_selector("button#login-apply-button")
        if login_apply and await login_apply.is_visible():
            return True
    else:
        if "login" in url or "signin" in url or "sign-in" in url:
            return True

    try:
        text = (await page.inner_text("body")).lower()
    except Exception:
        return False
    logged_out_markers = (
        "login to apply",
        "sign in to apply",
        "sign in to continue",
        "log in to continue",
        "please login",
        "please log in",
    )
    return any(marker in text for marker in logged_out_markers)

# ---------------------------------------------------------------------------
# Smart form auto-filler
# ---------------------------------------------------------------------------

async def _autofill_fields(page: Page, cfg: AppConfig) -> int:
    """
    Scan all visible input/select/textarea fields on the page and fill them
    using the autofill config via JavaScript for reliability.
    Returns the number of fields filled.
    """
    af = cfg.autofill
    if not af:
        return 0

    # Build the full answer map to pass into JS
    answers: dict[str, str] = {
        "name": cfg.name,
        "email": cfg.email,
        "phone": cfg.phone,
        "gender": af.get("gender", ""),
        "date_of_birth": af.get("date_of_birth", ""),
        "dob": af.get("date_of_birth", ""),
        "notice_period": af.get("notice_period", ""),
        "notice period": af.get("notice_period", ""),
        "current_ctc": af.get("current_ctc", ""),
        "current ctc": af.get("current_ctc", ""),
        "current salary": af.get("current_ctc", ""),
        "expected_ctc": af.get("expected_ctc", ""),
        "expected ctc": af.get("expected_ctc", ""),
        "expected salary": af.get("expected_ctc", ""),
        "total_experience": af.get("total_experience", ""),
        "total experience": af.get("total_experience", ""),
        "years of experience": af.get("total_experience", ""),
        "overall experience": af.get("total_experience", ""),
        "location": af.get("preferred_locations", [""])[0] if af.get("preferred_locations") else cfg.location,
        "city": af.get("preferred_locations", [""])[0] if af.get("preferred_locations") else cfg.location,
        "preferred location": af.get("preferred_locations", [""])[0] if af.get("preferred_locations") else cfg.location,
        "current location": cfg.location,
        "bio": af.get("bio", ""),
        "about": af.get("bio", ""),
        "about me": af.get("bio", ""),
        "summary": af.get("bio", ""),
        "cover letter": af.get("cover_letter", af.get("bio", "")),
        "cover note": af.get("cover_letter", af.get("bio", "")),
        "why join": af.get("why_join", ""),
        "why us": af.get("why_join", ""),
        "why work": af.get("why_join", ""),
        "why should we hire you": af.get("why_fit", ""),
        "describe yourself": af.get("bio", ""),
        "linkedin": af.get("linkedin", ""),
        "linkedin url": af.get("linkedin", ""),
        "linkedin profile": af.get("linkedin", ""),
        "github": af.get("github", ""),
        "github url": af.get("github", ""),
        "portfolio": af.get("portfolio", ""),
        "website": af.get("website", ""),
    }
    # Add experience keywords
    for k, v in af.get("experience", {}).items():
        answers[k.lower()] = v

    pref_locs = af.get("preferred_locations", [])

    # ---- Use JavaScript to find and fill all visible fields ----
    filled = await page.evaluate('''(config) => {
        const answers = config.answers;
        const prefLocs = config.prefLocs;
        let filled = 0;

        function getContext(el) {
            const ph = (el.placeholder || "").toLowerCase();
            const nm = (el.name || "").toLowerCase();
            const ar = (el.getAttribute("aria-label") || "").toLowerCase();
            const id = el.id || "";
            let lbl = "";
            if (id) {
                const labelEl = document.querySelector('label[for="' + id + '"]');
                if (labelEl) lbl = labelEl.textContent.toLowerCase();
            }
            // Also check parent/sibling text
            const parent = el.closest("div, li, td, span, label");
            const parentText = parent ? parent.textContent.toLowerCase().substring(0, 200) : "";
            return (ph + " " + nm + " " + ar + " " + lbl + " " + parentText).toLowerCase();
        }

        function isVisible(el) {
            return el.offsetParent !== null || el.offsetWidth > 0 || el.offsetHeight > 0;
        }

        function triggerChange(el) {
            el.dispatchEvent(new Event("input", {bubbles: true}));
            el.dispatchEvent(new Event("change", {bubbles: true}));
        }

        // --- Text/number/tel inputs and textareas ---
        const inputs = document.querySelectorAll(
            'input[type="text"], input[type="number"], input[type="tel"], ' +
            'input:not([type]), textarea'
        );
        for (const inp of inputs) {
            if (!isVisible(inp)) continue;
            if (inp.value && inp.value.trim()) continue;
            // Skip search bars
            if (inp.placeholder && /search|keyword/i.test(inp.placeholder)) continue;

            const ctx = getContext(inp);
            let matched = false;

            // Try each answer key against the context
            for (const [key, val] of Object.entries(answers)) {
                if (!val) continue;
                const parts = key.split(/\s+/);
                if (parts.every(p => ctx.includes(p))) {
                    // Extra guard: don't fill "company name" with person name
                    if (key === "name" && (ctx.includes("company") || ctx.includes("job"))) continue;
                    try {
                        const proto = inp.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                        if (setter) {
                            setter.call(inp, val);
                        } else {
                            inp.value = val;
                        }
                    } catch (err) {
                        try { inp.value = val; } catch (_) {}
                    }
                    triggerChange(inp);
                    filled++;
                    matched = true;
                    break;
                }
            }
        }

        // --- Select dropdowns ---
        const selects = document.querySelectorAll("select");
        for (const sel of selects) {
            if (!isVisible(sel)) continue;
            const ctx = getContext(sel);
            const options = Array.from(sel.options).map(o => ({
                text: o.textContent.toLowerCase().trim(),
                value: o.value
            }));

            let chosen = null;

            if (ctx.includes("gender")) {
                const target = (answers.gender || "male").toLowerCase();
                chosen = options.find(o => o.text.includes(target));
            } else if (ctx.includes("location") || ctx.includes("city") || ctx.includes("preferred")) {
                for (const pref of prefLocs) {
                    chosen = options.find(o => o.text.includes(pref.toLowerCase()));
                    if (chosen) break;
                }
            } else if (ctx.includes("notice")) {
                const target = (answers.notice_period || "immediate").toLowerCase();
                chosen = options.find(o => o.text.includes(target));
            } else if (ctx.includes("experience") || ctx.includes("exp")) {
                chosen = options.find(o => /[34]/.test(o.text));
            }

            if (chosen) {
                sel.value = chosen.value;
                triggerChange(sel);
                filled++;
            }
        }

        // --- Radio buttons ---
        const radios = document.querySelectorAll('input[type="radio"]');
        const radioGroups = {};
        for (const r of radios) {
            if (!isVisible(r)) continue;
            const name = r.name || "";
            if (!radioGroups[name]) radioGroups[name] = [];
            const label = (r.closest("label") || r.parentElement);
            const labelText = label ? label.textContent.toLowerCase().trim() : "";
            radioGroups[name].push({el: r, value: (r.value || "").toLowerCase(), label: labelText});
        }
        for (const [name, group] of Object.entries(radioGroups)) {
            const ctx = name.toLowerCase();
            let target = null;
            if (ctx.includes("gender")) target = (answers.gender || "male").toLowerCase();
            else if (ctx.includes("location") || ctx.includes("remote")) target = prefLocs[0]?.toLowerCase();

            if (target) {
                const match = group.find(r => r.value.includes(target) || r.label.includes(target));
                if (match) {
                    match.el.checked = true;
                    match.el.dispatchEvent(new Event("change", {bubbles: true}));
                    filled++;
                }
            }
        }

        return filled;
    }''', {"answers": answers, "prefLocs": pref_locs})

    if filled:
        logger.info("Autofill: filled %d fields", filled)
    return filled


# ---------------------------------------------------------------------------
# Resume upload helper
# ---------------------------------------------------------------------------

async def _upload_resume(page: Page, resume_path: str) -> bool:
    """
    Find any file-upload input on the page and upload the resume.
    Uses multiple strategies for maximum compatibility.
    Returns True if a file was uploaded.
    """
    # Strategy 1: Make ALL file inputs fully visible and interactable
    count = await page.evaluate('''() => {
        const inputs = document.querySelectorAll('input[type="file"]');
        for (const inp of inputs) {
            inp.style.cssText = "display:block !important; visibility:visible !important; opacity:1 !important; width:100px !important; height:30px !important; position:relative !important; z-index:99999 !important;";
            // Also make parents visible
            let parent = inp.parentElement;
            for (let i = 0; i < 5 && parent; i++) {
                parent.style.overflow = "visible";
                parent.style.display = "block";
                parent = parent.parentElement;
            }
        }
        return inputs.length;
    }''')

    if count > 0:
        # Try page.set_input_files with selector (more reliable than element handle)
        try:
            await page.set_input_files("input[type='file']", resume_path)
            logger.info("Resume uploaded via page.set_input_files")
            await page.wait_for_timeout(2000)
            return True
        except Exception as e:
            logger.debug("page.set_input_files failed: %s", e)

        # Fallback: try each file input element
        file_inputs = await page.query_selector_all("input[type='file']")
        for fi in file_inputs:
            try:
                await fi.set_input_files(resume_path)
                logger.info("Resume uploaded via element.set_input_files")
                await page.wait_for_timeout(2000)
                return True
            except Exception as e:
                logger.debug("element.set_input_files failed: %s", e)

    # Strategy 2: Click upload button/label and intercept file chooser
    upload_selectors = [
        "button:has-text('Upload')", "button:has-text('Attach')",
        "a:has-text('Upload Resume')", "a:has-text('Attach Resume')",
        "label:has-text('Upload')", "label:has-text('Attach')",
        "span:has-text('Upload Resume')", "span:has-text('upload resume')",
        "div:has-text('Upload Resume')",
        "label[for*='file']", "label[for*='resume']", "label[for*='upload']",
    ]
    for sel in upload_selectors:
        btn = await page.query_selector(sel)
        if btn:
            try:
                visible = await btn.is_visible()
                if not visible:
                    continue
                async with page.expect_file_chooser(timeout=5000) as fc_info:
                    await btn.click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(resume_path)
                logger.info("Resume uploaded via file chooser (%s)", sel)
                await page.wait_for_timeout(2000)
                return True
            except Exception:
                pass

    # Strategy 3: Click ANY element that mentions upload/resume and intercept
    try:
        upload_el = await page.evaluate('''() => {
            const els = document.querySelectorAll('*');
            for (const el of els) {
                if (el.offsetParent === null) continue;
                const t = el.textContent.trim().toLowerCase();
                if (t.length < 50 && (t.includes('upload resume') || t.includes('attach resume') || t === 'upload' || t === 'attach')) {
                    return true;
                }
            }
            return false;
        }''')
        if upload_el:
            async with page.expect_file_chooser(timeout=5000) as fc_info:
                await page.click("text=/upload|attach/i")
            file_chooser = await fc_info.value
            await file_chooser.set_files(resume_path)
            logger.info("Resume uploaded via text-match click")
            await page.wait_for_timeout(2000)
            return True
    except Exception:
        pass

    return False


# ---------------------------------------------------------------------------
# CAPTCHA detection helper
# ---------------------------------------------------------------------------

async def _detect_captcha(page: Page) -> bool:
    """Check VISIBLE page text for CAPTCHA indicators (avoids false positives from scripts)."""
    try:
        text = (await page.inner_text("body")).lower()
    except Exception:
        text = (await page.content()).lower()
    indicators = ("recaptcha", "hcaptcha", "cf-challenge", "verify you are human")
    return any(ind in text for ind in indicators)


# ---------------------------------------------------------------------------
# Per-platform apply helpers
# ---------------------------------------------------------------------------

def _experience_answer(q: str, af: dict, exp_map: dict) -> str:
    """
    Years-of-experience answer for a question (already lower-cased).

    A question naming a specific technology we don't list must NOT inherit
    the blanket total-experience figure — that would claim years of
    experience in tools the candidate has never used. Those answer 0.
    Only genuinely total/overall questions get the blanket number.
    """
    for kw, yrs in exp_map.items():
        if kw in q:
            return str(yrs)
    total = str(af.get("total_experience", "4"))
    if re.search(r"\b(total|overall|cumulative|relevant|professional)\b", q):
        return total

    # Only answer 0 when the question actually names a subject we don't
    # recognise. A bare preposition is not enough — "experience in years"
    # and "experience in this role" are generic phrasings, and treating
    # them as unknown skills wrongly answered 0 for real DevOps questions.
    m = re.search(r"\b(?:with|in|using|as an?)\s+([a-z0-9 .+#/&-]{2,40})", q)
    if not m:
        return total
    subject = m.group(1)
    subject = re.sub(r"\b(years?|yrs?|experience|exp|do|you|have|the|a|an)\b", " ", subject)
    subject = re.sub(r"\s+", " ", subject).strip(" ?.,")
    generic = {
        "", "this role", "role", "similar role", "field", "this field",
        "industry", "this industry", "domain", "this domain", "it",
        "software", "technology", "tech", "same", "this", "total",
    }
    if subject in generic:
        return total
    return "0"


def _generate_open_ended_answer(
    question: str, af: dict, cfg: AppConfig, job_title: str = ""
) -> str:
    """
    Intelligently answers open-ended subjective questions:
    'Why do you want to join?', 'Describe yourself', 'Bio', 'Why are you a good fit?'.
    Tries Groq LLM if API key is configured in environment for a job-specific answer,
    or falls back to curated answers from config.
    """
    q = question.lower().strip()

    # 1. Why join / why this company / why apply / what excites you
    if re.search(r"why.*(join|work|company|us|apply|hire)|interest in.*(role|company|position)|what excites you", q):
        if af.get("why_join"):
            return af["why_join"]
        return (
            "I am excited by your company's focus and technical mission. My hands-on background in "
            "Computer Vision, Deep Learning, and deploying AI models on edge hardware and cloud infrastructure "
            "directly aligns with this role, and I am eager to contribute immediately."
        )

    # 2. Describe yourself / bio / tell me about yourself / summary / cover note
    if re.search(r"describe yourself|about yourself|tell me about|bio|summary|introduction|profile summary|brief overview", q):
        if af.get("bio"):
            return af["bio"]
        return (
            "AI Engineer with experience designing and deploying end-to-end deep learning pipelines, "
            "Computer Vision models (YOLO, SegFormer), and LLM/RAG solutions with production edge inference on NVIDIA Jetson."
        )

    # 3. Why are you a good fit / suitability / strengths
    if re.search(r"why.*fit|good fit|suitable|qualif|strength", q):
        if af.get("why_fit"):
            return af["why_fit"]
        return (
            "My hands-on experience taking AI models from training to production deployment on edge hardware "
            "and cloud environments directly matches the requirements of this role."
        )

    # 4. Try Groq dynamic completion if GROQ_API_KEYS is available
    import os
    groq_keys = os.getenv("GROQ_API_KEYS", os.getenv("GROQ_API_KEY", ""))
    if groq_keys:
        try:
            from groq import Groq
            keys = [k.strip() for k in groq_keys.split(",") if k.strip() and not k.startswith("gsk_your_")]
            if keys:
                client = Groq(api_key=keys[0])
                model_name = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
                prompt = (
                    f"Candidate Profile: {cfg.name}, AI Engineer, 2 years experience in Computer Vision (PyTorch, YOLO, OpenCV) and LLMs (RAG, LangChain).\n"
                    f"Job Title / Context: {job_title or 'AI Engineer'}\n"
                    f"Question: {question}\n"
                    f"Answer this job application question directly and concisely in 2-3 professional sentences. Do not use placeholders."
                )
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=150,
                    temperature=0.3,
                )
                ans = resp.choices[0].message.content.strip()
                if ans and len(ans) > 10:
                    return ans
        except Exception:
            pass

    return af.get("bio", "Experienced AI Engineer specializing in Computer Vision, Deep Learning, and production model deployment.")


def _classify_linkedin_text_answer(
    label: str, af: dict, exp_map: dict, cfg: AppConfig,
) -> str:
    """
    Decide what to type into a LinkedIn Easy Apply text/number question,
    given its question label (already lower-cased). Mirrors the Naukri
    chatbot classification logic so both platforms answer consistently.
    Never returns empty — always answers something rather than skip.
    """
    q = label.lower().strip()
    if "current" in q and "ctc" in q:
        return str(af.get("current_ctc", "9"))
    if "expected" in q and ("ctc" in q or "salary" in q):
        return str(af.get("expected_ctc", "16"))
    if "ctc" in q or "salary" in q or "lpa" in q or "compensation" in q:
        return str(af.get("current_ctc", "9") if "current" in q else af.get("expected_ctc", "16"))
    # \bexp\b catches the common "total IT Exp" abbreviation. The word
    # boundary matters: a bare "exp" substring would also match "expected",
    # which the CTC branches above must keep owning.
    if "experience" in q or "years" in q or re.search(r"\bexp\b", q):
        return _experience_answer(q, af, exp_map)
    if "notice" in q and "negotia" in q:
        # Yes/no phrasing ("is your notice period negotiable?") vs. asking
        # for the actual number of negotiable days — the latter always
        # includes an explicit quantity cue like "how many"/"number of".
        if re.search(r"how many|number of", q):
            negotiable_days = af.get("notice_period_negotiable_days", "30")
            m = re.search(r"\d+", negotiable_days)
            return m.group(0) if m else negotiable_days
        return af.get("notice_period_negotiable", "Yes")
    if "notice" in q:
        notice = af.get("notice_period", "60 days")
        if "day" in q or "how many" in q:
            m = re.search(r"\d+", notice)
            return m.group(0) if m else notice
        return notice
    if re.search(r"last working day|lwd|when.*leave|when.*available|when.*join", q):
        return af.get("last_working_day", "Currently Working")
    if re.search(r"primary cloud|preferred cloud|main cloud|cloud platform|which cloud", q):
        return af.get("primary_cloud", "GCP")
    if re.search(r"contract|contractual|c2h|contract.based|contract to hire", q):
        return af.get("contract_based", "Yes")
    if re.search(r"how many (organi[sz]ations|companies|employers)|number of (organi[sz]ations|companies|employers)", q):
        return str(af.get("total_organizations", "2"))
    if re.search(r"certificat|certified", q):
        certs = [c.lower() for c in af.get("certifications", [])]
        return "Yes" if any(c and c in q for c in certs) else "No"
    if re.search(r"current status|employment status|serving notice|resigned", q):
        return af.get("current_status", "").split(",")[0].strip() or "Employed"
    if "phone" in q or "mobile" in q:
        return cfg.phone
    if "location" in q or "city" in q:
        # "preferred location" = job placement preference; a bare
        # "location"/"city" field is almost always asking for the
        # candidate's actual city of residence, where a job-preference
        # value like "Remote" would be an invalid (non-)answer.
        if "preferred" in q:
            pref = af.get("preferred_locations", [])
            if pref:
                return pref[0]
        return cfg.location
    if "name" in q and "company" not in q:
        return cfg.name
    if "age" in q:
        return "25"
    # Open-ended questions (why join, bio, describe yourself, suitability)
    if re.search(r"why.*(join|work|company|us|apply|hire)|interest in|describe yourself|about yourself|bio|summary|introduction|profile summary|why.*fit|good fit", q):
        return _generate_open_ended_answer(label, af, cfg)
    # Never leave a question unanswered — default to Yes for anything else
    # (most unclassified Easy Apply screening questions are yes/no gates).
    return "Yes"


def _classify_linkedin_select(
    label: str, options: list[str], af: dict, exp_map: dict, cfg: AppConfig,
) -> str | None:
    """
    Pick an option for a LinkedIn <select> screening question.

    Factual questions (currently serving notice, currently in <city>,
    holding a certification) are answered from config so we never
    misrepresent the candidate; willingness-style questions default to Yes.
    Returns the option text to select, or None if there's nothing to pick.
    """
    q = label.lower()
    placeholders = {"", "-", "select an option", "please select", "choose an option"}
    real = [o for o in options if o.strip().lower() not in placeholders]
    if not real:
        return None

    def pick(prefix: str) -> str | None:
        return next((o for o in real if o.strip().lower().startswith(prefix)), None)

    yes, no = pick("yes"), pick("no")

    if yes and no and len(real) <= 3:
        # --- factual: are you currently serving notice? ---
        if re.search(r"serving\s+(the\s+)?notice", q):
            status = (af.get("current_status") or "").lower()
            resigned = "resigned" in status and "not resigned" not in status
            return yes if (resigned or "serving" in status) else no
        # --- factual: are you currently in <city>? ---
        m = re.search(r"currently\s+(?:in|based in|located in|residing in)\s+([a-z\s]+)", q)
        if m:
            city = m.group(1).strip().rstrip("?").strip()
            home = (cfg.location or "").lower()
            return yes if city and city in home else no
        # --- factual: do you hold certification X? ---
        if re.search(r"certificat|certified", q):
            certs = [c.lower() for c in af.get("certifications", [])]
            return yes if any(c and c in q for c in certs) else no
        # Everything else on a yes/no gate (willingness, relocation,
        # background check, notice negotiable) answers Yes.
        return yes

    # Numeric option lists (e.g. years of experience) — pick the closest
    # option to the configured figure, preferring a skill-specific value.
    ranges: list[tuple[float, float, str]] = []
    for o in real:
        found = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", o)]
        if found:
            lo, hi = found[0], (found[1] if len(found) > 1 else found[0])
            # "5+ years" is open-ended upward.
            if "+" in o and len(found) == 1:
                hi = float("inf")
            ranges.append((lo, hi, o))
    if ranges:
        try:
            target = float(_experience_answer(q, af, exp_map))
        except (TypeError, ValueError):
            target = float(af.get("total_experience", "4") or 4)
        # Prefer the bucket the target actually falls inside ("2-5 years"
        # for 4) before falling back to the nearest midpoint.
        inside = [r for r in ranges if r[0] <= target <= r[1]]
        if inside:
            return min(inside, key=lambda r: r[1] - r[0])[2]
        return min(ranges, key=lambda r: abs((r[0] + min(r[1], r[0] + 20)) / 2 - target))[2]

    return real[0]


async def _apply_linkedin(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Apply via LinkedIn Easy Apply, answering screening questions like Naukri."""
    try:
        af = cfg.autofill or {}
        exp_map = dict(
            sorted(
                ((k.lower(), v) for k, v in af.get("experience", {}).items()),
                key=lambda kv: -len(kv[0]),
            )
        )
        qa_log: list[dict[str, str]] = []

        # The Easy Apply control is NOT always a <button>: LinkedIn also
        # ships a variant that renders it as an <a>. Search buttons, links
        # and role=button alike.
        #
        # Match the text EXACTLY ('easy apply'), never as a substring — the
        # "similar jobs" cards further down the page also contain the words
        # "Easy Apply" inside a long label, and clicking one of those would
        # navigate to a completely different job.
        #
        # Don't gate on className either: LinkedIn's class names are build
        # hashes, so the old 'jobs-apply' check could never match.
        clicked = None
        for _ in range(12):
            clicked = await page.evaluate('''() => {
                const els = document.querySelectorAll('button, a, [role="button"]');
                for (const el of els) {
                    if (el.offsetParent === null) continue;
                    const aria = (el.getAttribute('aria-label') || '').trim().toLowerCase();
                    const text = (el.textContent || '').trim().toLowerCase();
                    if (aria.startsWith('easy apply to') || text === 'easy apply') {
                        const href = el.tagName === 'A' ? el.getAttribute('href') : null;
                        if (!href) el.click();
                        return {href: href};
                    }
                }
                return null;
            }''')
            if clicked:
                break
            await page.wait_for_timeout(500)

        if not clicked:
            return {"success": False, "error": "No Easy Apply button — external apply, skipped"}

        # When the control is an <a>, a synthetic .click() is a no-op —
        # navigate to its href (LinkedIn's server-driven apply flow URL)
        # instead, otherwise nothing ever happens and we'd wrongly report
        # the job as external-apply.
        href = clicked.get("href")
        if href:
            if href.startswith("/"):
                href = "https://www.linkedin.com" + href
            await page.goto(href, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2000)

        # LinkedIn ships two apply-flow variants: a native <dialog> opened
        # via showModal(), and a server-driven flow that renders a plain
        # container with role="dialog". Handle both. Playwright's
        # wait_for(state="visible") is unreliable for native <dialog>, so
        # poll instead: a <dialog> counts as open only when .open is true,
        # while a role="dialog" container just has to be present.
        modal = page.locator('dialog, [role="dialog"]').first
        modal_open = False
        for _ in range(20):
            modal_open = await page.evaluate("""() => {
                const d = document.querySelector('dialog');
                if (d) return !!d.open;
                return !!document.querySelector('[role="dialog"]');
            }""")
            if modal_open:
                break
            await page.wait_for_timeout(500)
        if not modal_open:
            return {"success": False, "error": "Easy Apply modal did not open in time"}

        for step in range(10):
            if await modal.count() == 0:
                break

            # The dialog can report open=True (or a page transition can
            # complete) before React hydrates its actual content — a lone
            # progress bar or loading text can appear before any real input
            # or actionable button exists, so require one of THOSE
            # specifically, not just any dialog content.
            for _ in range(20):
                has_content = await page.evaluate(
                    """() => {
                        const d = (document.querySelector('dialog') || document.querySelector('[role="dialog"]'));
                        if (!d) return false;
                        if (d.querySelector('input')) return true;
                        const btns = Array.from(d.querySelectorAll('button')).map(b => b.textContent.trim());
                        return btns.some(t => t === 'Next' || t === 'Review' || t === 'Submit application');
                    }"""
                )
                if has_content:
                    break
                await page.wait_for_timeout(400)
            if has_content:
                await page.wait_for_timeout(500)

            text = (await modal.inner_text()).lower()
            if "application sent" in text or "application submitted" in text or "your application was sent" in text:
                return {"success": True, "confirmation": f"LinkedIn Easy Apply submitted (answered {len(qa_log)} questions)", "qa_log": qa_log}

            # --- Resume page: make sure OUR configured resume is selected ---
            resume_name = Path(cfg.resume_path).name if cfg.resume_exists else ""
            if resume_name:
                card = modal.get_by_text(resume_name, exact=False)
                if await card.count() > 0:
                    is_selected = await page.evaluate(
                        """(name) => {
                            const modal = (document.querySelector('dialog') || document.querySelector('[role="dialog"]'));
                            const radios = modal.querySelectorAll('input[type="radio"]');
                            for (const r of radios) {
                                const card = r.closest('div[role="button"], div');
                                if (card && card.textContent.includes(name)) return r.checked;
                            }
                            return null;
                        }""",
                        resume_name,
                    )
                    if is_selected is False:
                        try:
                            await card.first.click()
                        except Exception:
                            pass
                elif cfg.resume_exists:
                    file_input = modal.locator("input[type='file']")
                    if await file_input.count() > 0:
                        await file_input.first.set_input_files(cfg.resume_path)
                        await page.wait_for_timeout(1000)

            # --- Fill empty required text/number/tel inputs ---
            # Query ALL non-special-purpose inputs and check the resolved
            # `.type` IDL property (defaults to "text"), rather than an
            # attribute-selector like input[type="text"] — some fields
            # (e.g. a "Location (city)" autocomplete) omit the type
            # attribute entirely and were silently invisible to this query.
            fields = await page.evaluate("""() => {
                const modal = (document.querySelector('dialog') || document.querySelector('[role="dialog"]'));
                if (!modal) return [];
                const skip = new Set(['radio', 'checkbox', 'file', 'hidden', 'submit', 'button', 'image', 'range', 'color', 'date', 'time', 'datetime-local']);
                const inputs = Array.from(modal.querySelectorAll('input')).filter(el => !skip.has(el.type));
                inputs.push(...modal.querySelectorAll('textarea'));
                const out = [];
                inputs.forEach(el => {
                    if (el.offsetParent === null) return;
                    let label = el.getAttribute('aria-label') || '';
                    if (!label && el.id) {
                        const lbl = modal.querySelector('label[for="' + el.id + '"]');
                        if (lbl) label = lbl.textContent.trim();
                    }
                    out.push({id: el.id, label, value: el.value});
                });
                return out;
            }""")
            for f in fields:
                if f["value"] or not f["label"]:
                    continue
                value = _classify_linkedin_text_answer(f["label"].lower(), af, exp_map, cfg)
                locator = modal.locator(f'[id="{f["id"]}"]')
                try:
                    await locator.fill(value)
                    qa_log.append({"question": f["label"][:120], "answer": value, "type": "text"})
                except Exception as exc:
                    logger.warning("LinkedIn: failed to fill %r: %s", f["label"], exc)

            # --- Radio-button questions ---
            # LinkedIn wraps each option in a custom div[role="radio"] whose
            # OWN aria-label holds the real option text ("Yes"/"No") — the
            # native <input>'s associated <label for=""> exists but is
            # empty, and the fieldset has no <legend>; the actual question
            # text is a <p> sibling immediately before the fieldset. Decide
            # and click within a single evaluate call (like the Naukri
            # radio logic) so the click lands on the real interactive
            # element rather than a possibly non-interactive native input.
            answered_radios = await page.evaluate("""(config) => {
                const af = config.af;
                const expMap = config.expMap;
                const modal = (document.querySelector('dialog') || document.querySelector('[role="dialog"]'));
                if (!modal) return [];
                const answered = [];
                modal.querySelectorAll('fieldset').forEach(fs => {
                    const radios = fs.querySelectorAll('input[type="radio"]');
                    if (!radios.length) return;
                    if (Array.from(radios).some(r => r.checked)) return;

                    let question = '';
                    const legend = fs.querySelector('legend');
                    if (legend) question = legend.textContent.trim();
                    if (!question) question = fs.getAttribute('aria-label') || '';
                    if (!question) {
                        const prev = fs.previousElementSibling;
                        if (prev && prev.tagName === 'P') question = prev.textContent.trim();
                    }
                    const q = question.toLowerCase();

                    const options = Array.from(radios).map(r => {
                        const wrapper = r.closest('[role="radio"]');
                        let label = wrapper ? (wrapper.getAttribute('aria-label') || '') : '';
                        if (!label) {
                            const lbl = document.querySelector('label[for="' + r.id + '"]');
                            label = lbl ? lbl.textContent.trim() : '';
                        }
                        return {clickTarget: wrapper || r, label: label || r.value || ''};
                    });

                    // Prefer the option whose range CONTAINS the target
                    // ("2-5 years" for 4) rather than the nearest midpoint,
                    // which could land on a "0-2"/no-experience bucket.
                    function pickRange(options, target) {
                        const parsed = [];
                        options.forEach(o => {
                            const nums = (o.label.match(/\d+(\.\d+)?/g) || []).map(Number);
                            if (!nums.length) return;
                            const lo = nums[0];
                            let hi = nums.length >= 2 ? nums[1] : nums[0];
                            if (/\+/.test(o.label) && nums.length === 1) hi = Infinity;
                            parsed.push({lo: lo, hi: hi, o: o});
                        });
                        if (!parsed.length) return null;
                        const inside = parsed.filter(p => p.lo <= target && target <= p.hi);
                        if (inside.length) {
                            inside.sort((a, b) => (a.hi - a.lo) - (b.hi - b.lo));
                            return inside[0].o;
                        }
                        let best = null, bestDiff = Infinity;
                        parsed.forEach(p => {
                            const mid = (p.lo + Math.min(p.hi, p.lo + 20)) / 2;
                            const d = Math.abs(mid - target);
                            if (d < bestDiff) { bestDiff = d; best = p.o; }
                        });
                        return best;
                    }

                    let chosen = null;
                    if (/certificat|certified/.test(q)) {
                        const certs = (af.certifications || []).map(c => c.toLowerCase());
                        const hasCert = certs.some(c => c && q.includes(c));
                        chosen = options.find(o => hasCert ? /^yes/i.test(o.label) : /^no/i.test(o.label));
                    } else if (/relocat/.test(q)) {
                        for (const pref of (af.preferred_locations || [])) {
                            chosen = options.find(o => o.label.toLowerCase().includes(pref.toLowerCase()));
                            if (chosen) break;
                        }
                        if (!chosen) chosen = options.find(o => /^yes/i.test(o.label));
                    } else if (/gender/.test(q)) {
                        const target = (af.gender || 'male').toLowerCase();
                        chosen = options.find(o => o.label.toLowerCase().includes(target));
                    } else if (/experience|years/.test(q)) {
                        let target = parseFloat(af.total_experience || '4');
                        let matched = false;
                        for (const kw in expMap) {
                            if (q.includes(kw)) { target = parseFloat(expMap[kw]); matched = true; break; }
                        }
                        // A named technology we don't list must not inherit
                        // the blanket total — target 0 instead of claiming
                        // years in a tool the candidate hasn't used.
                        if (!matched && !/\b(total|overall|cumulative|relevant|professional)\b/.test(q)) {
                            const sm = q.match(/\b(?:with|in|using|as an?)\s+([a-z0-9 .+#\/&-]{2,40})/);
                            if (sm) {
                                const subj = sm[1]
                                    .replace(/\b(years?|yrs?|experience|exp|do|you|have|the|a|an)\b/g, ' ')
                                    .replace(/\s+/g, ' ').trim().replace(/[?.,]+$/, '');
                                const generic = ['', 'this role', 'role', 'similar role', 'field',
                                    'this field', 'industry', 'this industry', 'domain', 'this domain',
                                    'it', 'software', 'technology', 'tech', 'same', 'this', 'total'];
                                if (subj && generic.indexOf(subj) === -1) target = 0;
                            }
                        }
                        chosen = pickRange(options, target);
                    } else if (/notice/.test(q) && /negotia/.test(q)) {
                        // "Is your notice period negotiable?" (yes/no) vs.
                        // "how many days is it negotiable?" (numeric).
                        if (/how many|number of/.test(q)) {
                            const target = parseFloat(af.notice_period_negotiable_days || '30');
                            let best = null, bestDiff = 999;
                            options.forEach(o => {
                                const nums = (o.label.match(/\\d+(\\.\\d+)?/g) || []).map(Number);
                                const n = nums.length ? (nums.length >= 2 ? (nums[0] + nums[1]) / 2 : nums[0]) : NaN;
                                if (!isNaN(n) && Math.abs(n - target) < bestDiff) { bestDiff = Math.abs(n - target); best = o; }
                            });
                            chosen = best;
                        } else {
                            chosen = options.find(o => /^yes/i.test(o.label));
                        }
                    } else if (/willing|comfortable|able to|do you|are you|can you|negotiable|agree/.test(q)) {
                        chosen = options.find(o => /^yes/i.test(o.label));
                    }
                    if (!chosen) {
                        // Never deliberately choose a "skip" option.
                        chosen = options.find(o => !/skip/i.test(o.label)) || options[0];
                    }
                    if (chosen) {
                        chosen.clickTarget.click();
                        answered.push({question: question.slice(0, 120), answer: chosen.label});
                    }
                });
                return answered;
            }""", {"af": af, "expMap": exp_map})
            for a in answered_radios:
                qa_log.append({"question": a["question"], "answer": a["answer"], "type": "radio"})

            # --- <select> dropdown questions ---
            # LinkedIn's server-driven apply flow renders many screening
            # questions as native selects left on a "Select an option"
            # placeholder. They're required, so leaving them unset silently
            # blocks the Review/Submit step forever.
            selects = await page.evaluate("""() => {
                const modal = (document.querySelector('dialog') || document.querySelector('[role="dialog"]'));
                if (!modal) return [];
                return Array.from(modal.querySelectorAll('select')).filter(s => s.offsetParent !== null).map(s => {
                    let label = s.getAttribute('aria-label') || '';
                    if (!label && s.id) {
                        const lbl = modal.querySelector('label[for="' + s.id + '"]');
                        if (lbl) label = lbl.textContent.trim();
                    }
                    if (!label) {
                        const p = s.closest('div');
                        if (p && p.previousElementSibling) label = p.previousElementSibling.textContent.trim();
                    }
                    return {
                        id: s.id, label: label,
                        value: s.value,
                        options: Array.from(s.options).map(o => o.text),
                    };
                });
            }""")
            placeholders = {"", "-", "select an option", "please select", "choose an option"}
            for s in selects:
                if (s.get("value") or "").strip().lower() not in placeholders:
                    continue  # already answered (e.g. prefilled email)
                if not s.get("label"):
                    continue
                choice = _classify_linkedin_select(s["label"], s["options"], af, exp_map, cfg)
                if not choice:
                    continue
                try:
                    await modal.locator(f'[id="{s["id"]}"]').select_option(label=choice)
                    qa_log.append({"question": s["label"][:120], "answer": choice, "type": "select"})
                except Exception as exc:
                    logger.warning("LinkedIn: failed to set select %r: %s", s["label"][:60], exc)

            await page.wait_for_timeout(500)

            # --- Advance: Next / Review / Submit application ---
            # Match on visible text OR aria-label. get_by_role(name=...)
            # matches the ACCESSIBLE name, which here is the aria-label
            # ("Continue to next step") rather than the visible text
            # ("Next"), so exact-matching the visible label found nothing.
            # Submit is checked first so we never click past it.
            advanced = await page.evaluate("""() => {
                const modal = (document.querySelector('dialog') || document.querySelector('[role="dialog"]'));
                if (!modal) return null;
                const els = Array.from(modal.querySelectorAll('button, [role="button"]'))
                    .filter(e => e.offsetParent !== null && !e.disabled);
                const match = (e, textRe, ariaRe) => {
                    const t = (e.textContent || '').trim().toLowerCase();
                    const a = (e.getAttribute('aria-label') || '').trim().toLowerCase();
                    return textRe.test(t) || ariaRe.test(a);
                };
                const submit = els.find(e => match(e, /^submit application$/, /^submit application/));
                if (submit) { submit.click(); return 'submit'; }
                const review = els.find(e => match(e, /^review$/, /^review your application/));
                if (review) { review.click(); return 'review'; }
                const next = els.find(e => match(e, /^next$/, /^continue to next step/));
                if (next) { next.click(); return 'next'; }
                return null;
            }""")

            if advanced == "submit":
                await page.wait_for_timeout(3000)
                return {"success": True, "confirmation": f"LinkedIn Easy Apply submitted (answered {len(qa_log)} questions)", "qa_log": qa_log}
            elif advanced is None:
                break
            await page.wait_for_timeout(2000)

        # The loop exited without ever reaching a submit confirmation — the
        # application was never actually sent, so this must not be reported
        # as success (that would falsely mark it "applied" in the tracker
        # DB and block any future retry).
        return {
            "success": False,
            "error": f"LinkedIn apply flow did not complete — stuck before submit (answered {len(qa_log)} questions)",
            "qa_log": qa_log,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _naukri_login(page: Page, cfg: AppConfig) -> bool:
    """Log in to Naukri inline if not already authenticated."""
    creds = cfg.credentials.get("naukri", {})
    email = creds.get("email", "")
    password = creds.get("password", "")
    if not email or not password:
        return False

    # Click "Login to apply" if visible
    login_btn = await page.query_selector("button#login-apply-button")
    if not login_btn or not await login_btn.is_visible():
        return True  # already logged in

    await login_btn.click()
    await page.wait_for_timeout(2000)

    # Fill login form
    email_input = await page.query_selector(
        "input[type='email'], input[placeholder*='Email'], input[id*='usernameField']"
    )
    pass_input = await page.query_selector(
        "input[type='password'], input[placeholder*='Password'], input[id*='passwordField']"
    )
    if email_input and pass_input:
        await email_input.fill(email)
        await pass_input.fill(password)
        submit = await page.query_selector(
            "button[type='submit'], button[class*='loginButton'], button:has-text('Login')"
        )
        if submit:
            await submit.click()
            await page.wait_for_timeout(4000)
            logger.info("Naukri: login submitted")
    return True


async def _apply_naukri(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Apply on Naukri.com."""
    try:
        # Check if logged in — if "Login to apply" is visible, log in first
        login_btn = await page.query_selector("button#login-apply-button")
        if login_btn and await login_btn.is_visible():
            logged_in = await _naukri_login(page, cfg)
            if not logged_in:
                return {"success": False, "error": "Naukri login failed — check credentials in config.json"}
            # Reload the job page after login
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

        # Check if this is an external-apply job — skip without clicking
        is_external = await page.evaluate('''() => {
            const extBtn = document.querySelector('button#company-site-button');
            return !!(extBtn && extBtn.offsetParent !== null);
        }''')
        if is_external:
            return {"success": False, "error": "External apply (company site) — skipped"}

        # Click the direct Apply button only
        clicked = await page.evaluate('''() => {
            const btn = document.querySelector('button#apply-button, button.apply-button');
            if (btn) {
                btn.scrollIntoView();
                btn.click();
                return true;
            }
            return false;
        }''')

        if not clicked:
            return {"success": False, "error": "No direct apply button found"}

        await page.wait_for_timeout(3000)

        # --- Naukri chatbot / multi-step apply loop ---
        # Naukri chatbot uses:
        #   - contenteditable div (class="textArea") for text answers (NOT <input>)
        #   - radio buttons (class="ssrc__radio") for choice answers
        #   - div.sendMsg "Save" (NOT <button>) to submit each answer
        #   - input.chatbot_Uploader[type=file] for resume upload
        af = cfg.autofill or {}
        # Sort longest-keyword-first so specific skills (e.g. "azure devops")
        # are matched before shorter substrings they contain (e.g. "devops").
        exp_map = dict(
            sorted(
                ((k.lower(), v) for k, v in af.get("experience", {}).items()),
                key=lambda kv: -len(kv[0]),
            )
        )
        qa_log: list[dict[str, str]] = []
        answered = 0

        for step in range(15):
            await page.wait_for_timeout(2000)

            # Check if we're done — multiple success indicators
            text = (await page.inner_text("body")).lower()
            if "already applied" in text:
                return {"success": True, "confirmation": "Already applied to this job on Naukri", "qa_log": qa_log}
            if any(kw in text for kw in (
                "application submitted", "applied successfully",
                "applied to", "thank you for applying",
                "we have received your application",
                "your application has been sent",
                "successfully applied",
            )):
                return {"success": True, "confirmation": f"Naukri application submitted (answered {answered} questions)", "qa_log": qa_log}

            # Check if chatbot is closed or no more interactive elements
            chatbot_open = await page.evaluate("""() => {
                const wrapper = document.querySelector('div.chatbot_DrawerContentWrapper');
                if (!wrapper) return false;
                if (wrapper.offsetParent === null) return false;
                // Any interactive element?
                const ce = wrapper.querySelector('div[contenteditable="true"]');
                const radio = wrapper.querySelector('input[type="radio"]');
                const textInput = wrapper.querySelector('input[type="text"], input[type="number"], textarea');
                const file = wrapper.querySelector('input[type="file"]');
                return !!(ce || radio || textInput || file);
            }""")
            if step > 0 and not chatbot_open:
                # Chatbot closed or no more questions — successful application
                return {"success": True, "confirmation": f"Naukri application submitted (answered {answered} questions)", "qa_log": qa_log}

            # --- Read the LAST chatbot question only ---
            question = await page.evaluate("""() => {
                // Get only the last bot message (the current question)
                const botItems = document.querySelectorAll('li.botItem');
                if (botItems.length === 0) return '';
                const lastBot = botItems[botItems.length - 1];
                const span = lastBot.querySelector('span');
                return span ? span.textContent.trim().toLowerCase() : lastBot.textContent.trim().toLowerCase();
            }""")
            logger.info("Naukri chatbot step %d Q: %s", step + 1, question[:60])

            # === 1) RADIO BUTTONS (ssrc__radio) ===
            has_radios = await page.evaluate("""() => {
                return document.querySelectorAll('input[type="radio"]').length > 0 &&
                    Array.from(document.querySelectorAll('input[type="radio"]')).some(r => r.offsetParent !== null);
            }""")

            if has_radios:
                chosen = await page.evaluate("""(config) => {
                    const q = config.question;
                    const af = config.af;
                    const expMap = config.expMap;
                    const radios = document.querySelectorAll('input[type="radio"]');
                    const options = [];
                    radios.forEach(r => {
                        if (!r.offsetParent) return;
                        const lbl = document.querySelector('label[for="' + r.id + '"]');
                        options.push({el: r, value: r.value, label: lbl ? lbl.textContent.trim().toLowerCase() : r.value});
                    });
                    if (!options.length) return false;

                    // Extract a representative number from an option's visible
                    // label ("8-10 years" -> 9, "4+ years" -> 4), falling back
                    // to the radio's raw `value` attribute only if the label
                    // has no digits at all. The `value` attribute is often an
                    // internal option id/index, not the displayed number, so
                    // it must never be trusted over the label text.
                    function numFromOption(o) {
                        const nums = (o.label.match(/\\d+(\\.\\d+)?/g) || []).map(Number);
                        if (nums.length >= 2) return (nums[0] + nums[1]) / 2;
                        if (nums.length === 1) return nums[0];
                        const v = parseFloat(o.value);
                        return isNaN(v) ? NaN : v;
                    }

                    // Prefer the option whose range CONTAINS the target
                    // ("2-5 years" for 4) rather than the nearest midpoint,
                    // which could land on a "0-2"/no-experience bucket.
                    function pickRange(options, target) {
                        const parsed = [];
                        options.forEach(o => {
                            const nums = (o.label.match(/\d+(\.\d+)?/g) || []).map(Number);
                            if (!nums.length) return;
                            const lo = nums[0];
                            let hi = nums.length >= 2 ? nums[1] : nums[0];
                            if (/\+/.test(o.label) && nums.length === 1) hi = Infinity;
                            parsed.push({lo: lo, hi: hi, o: o});
                        });
                        if (!parsed.length) return null;
                        const inside = parsed.filter(p => p.lo <= target && target <= p.hi);
                        if (inside.length) {
                            inside.sort((a, b) => (a.hi - a.lo) - (b.hi - b.lo));
                            return inside[0].o;
                        }
                        let best = null, bestDiff = Infinity;
                        parsed.forEach(p => {
                            const mid = (p.lo + Math.min(p.hi, p.lo + 20)) / 2;
                            const d = Math.abs(mid - target);
                            if (d < bestDiff) { bestDiff = d; best = p.o; }
                        });
                        return best;
                    }

                    let chosen = null;

                    if (/notice/.test(q) && /negotia/.test(q)) {
                        // "Is your notice period negotiable?" (yes/no) vs.
                        // "how many days is it negotiable?" (numeric) —
                        // check BEFORE the general notice-period bucket
                        // matcher below, which would otherwise try to
                        // numeric-match Yes/No options and pick arbitrarily.
                        if (/how many|number of/.test(q)) {
                            const target = parseFloat(af.notice_period_negotiable_days || '30');
                            let best = null, bestDiff = 999;
                            options.forEach(o => {
                                const n = numFromOption(o);
                                if (!isNaN(n) && Math.abs(n - target) < bestDiff) { bestDiff = Math.abs(n - target); best = o; }
                            });
                            chosen = best || options[0];
                        } else {
                            chosen = options.find(o => /^yes|true/.test(o.label)) || options[0];
                        }
                    } else if (/immediate|joiner|notice|join/.test(q)) {
                        // Find closest option to notice period (default 15 days)
                        const noticeStr = af.notice_period || '15 days';
                        const targetDays = parseInt(noticeStr.match(/\\d+/)?.[0] || '15');
                        let best = null, bestDiff = 999;
                        options.forEach(o => {
                            const n = numFromOption(o);
                            if (!isNaN(n) && Math.abs(n - targetDays) < bestDiff) {
                                bestDiff = Math.abs(n - targetDays);
                                best = o;
                            }
                        });
                        chosen = best || options[0];
                    } else if (/gender/.test(q)) {
                        const target = (af.gender || 'male').toLowerCase();
                        chosen = options.find(o => o.label.includes(target)) || options[0];
                    } else if (/relocat/.test(q)) {
                        // Some jobs phrase this as "which location can you
                        // relocate to?" (options are city names) — try that
                        // first. Otherwise it's a yes/no willingness
                        // question, and the answer is always yes.
                        for (const pref of (af.preferred_locations || [])) {
                            chosen = options.find(o => o.label.includes(pref.toLowerCase()));
                            if (chosen) break;
                        }
                        chosen = chosen || options.find(o => /^yes|true/.test(o.label)) || options[0];
                    } else if (/location|city/.test(q)) {
                        for (const pref of (af.preferred_locations || [])) {
                            chosen = options.find(o => o.label.includes(pref.toLowerCase()));
                            if (chosen) break;
                        }
                    } else if (/certificat|certified/.test(q)) {
                        // Only claim a certification the candidate actually
                        // lists in config — default to "No" otherwise so we
                        // never misrepresent qualifications.
                        const certs = (af.certifications || []).map(c => c.toLowerCase());
                        const hasCert = certs.some(c => c && q.includes(c));
                        chosen = options.find(o => hasCert ? /^yes|true/.test(o.label) : /^no|false/.test(o.label)) || options[options.length - 1];
                    } else if (/willing|ready|agree|do you|are you|can you|comfortable|face to face|f2f|interview|onsite|in.person|office/.test(q)) {
                        chosen = options.find(o => /yes|true|0|agree/.test(o.label)) || options[0];
                    } else if (/current status|employment status|serving notice|resigned/.test(q)) {
                        // Try candidate substrings in order of specificity.
                        const candidates = (af.current_status || '').toLowerCase().split(',').map(s => s.trim()).filter(Boolean);
                        for (const c of candidates) {
                            chosen = options.find(o => o.label.includes(c));
                            if (chosen) break;
                        }
                        chosen = chosen || options[0];
                    } else if (/how many (organi[sz]ations|companies|employers)|number of (organi[sz]ations|companies|employers)|orgs (worked|till date)/.test(q)) {
                        const target = parseInt(af.total_organizations || '2');
                        let best = null, bestDiff = 999;
                        options.forEach(o => {
                            const n = numFromOption(o);
                            if (!isNaN(n) && Math.abs(n - target) < bestDiff) { bestDiff = Math.abs(n - target); best = o; }
                        });
                        chosen = best || options[0];
                    } else if (/experience|years/.test(q)) {
                        // Match a specific skill keyword first (expMap is
                        // pre-sorted longest-keyword-first), else fall back
                        // to the blanket total_experience figure.
                        let target = parseFloat(af.total_experience || '4');
                        let matched = false;
                        for (const kw in expMap) {
                            if (q.includes(kw)) { target = parseFloat(expMap[kw]); matched = true; break; }
                        }
                        // A named technology we don't list must not inherit
                        // the blanket total — target 0 instead of claiming
                        // years in a tool the candidate hasn't used.
                        if (!matched && !/\b(total|overall|cumulative|relevant|professional)\b/.test(q)) {
                            const sm = q.match(/\b(?:with|in|using|as an?)\s+([a-z0-9 .+#\/&-]{2,40})/);
                            if (sm) {
                                const subj = sm[1]
                                    .replace(/\b(years?|yrs?|experience|exp|do|you|have|the|a|an)\b/g, ' ')
                                    .replace(/\s+/g, ' ').trim().replace(/[?.,]+$/, '');
                                const generic = ['', 'this role', 'role', 'similar role', 'field',
                                    'this field', 'industry', 'this industry', 'domain', 'this domain',
                                    'it', 'software', 'technology', 'tech', 'same', 'this', 'total'];
                                if (subj && generic.indexOf(subj) === -1) target = 0;
                            }
                        }
                        chosen = pickRange(options, target);
                    }
                    if (!chosen) {
                        // Never deliberately pick a "Skip" option — always
                        // answer with something rather than leave it blank.
                        chosen = options.find(o => !o.label.includes('skip')) || options[0];
                    }
                    if (chosen) { chosen.el.click(); return {ok: true, label: chosen.label}; }
                    return {ok: false, label: null};
                }""", {"question": question, "af": af, "expMap": exp_map})
                if chosen and chosen.get("ok"):
                    answered += 1
                    qa_log.append({"question": question[:120], "answer": chosen.get("label", ""), "type": "radio"})

            # === 2) CONTENTEDITABLE DIV (Naukri chatbot text input) ===
            has_contenteditable = await page.evaluate("""() => {
                const ce = document.querySelector('div[contenteditable="true"].textArea, div[contenteditable="true"][data-placeholder]');
                return !!(ce && ce.offsetParent !== null);
            }""")

            if has_contenteditable:
                value = ""
                import re as _re
                # ORDER MATTERS: Check specific data fields BEFORE generic yes/no patterns.
                # CTC questions
                if "current" in question and "ctc" in question:
                    value = af.get("current_ctc", "9")
                elif "expected" in question and "ctc" in question:
                    value = af.get("expected_ctc", "16")
                elif "ctc" in question or "salary" in question or "lpa" in question or "compensation" in question:
                    value = af.get("current_ctc", "9") if "current" in question or "present" in question else af.get("expected_ctc", "16")
                # Experience questions (including "how many years of experience do you have in X")
                elif "experience" in question or "years" in question or ("year" in question and "exp" in question):
                    value = _experience_answer(question, af, exp_map)
                elif "notice" in question and "negotia" in question:
                    if _re.search(r"how many|number of", question):
                        negotiable_days = af.get("notice_period_negotiable_days", "30")
                        m = _re.search(r"\d+", negotiable_days)
                        value = m.group(0) if m else negotiable_days
                    else:
                        value = af.get("notice_period_negotiable", "Yes")
                elif "notice" in question:
                    notice = af.get("notice_period", "60 days")
                    # If it's a text input, type the full string. If asking days as number, extract digits.
                    if "day" in question or "how many" in question:
                        # Extract number from "60 days" → "60"
                        m = _re.search(r"\d+", notice)
                        value = m.group(0) if m else notice
                    else:
                        value = notice
                # Last Working Day (LWD) questions
                elif _re.search(r"last working day|lwd|last day|when.*leave|when.*available|when.*join", question):
                    value = af.get("last_working_day", "Currently Working")
                # Primary cloud / preferred cloud questions
                elif _re.search(r"primary cloud|preferred cloud|main cloud|cloud platform|which cloud", question):
                    value = af.get("primary_cloud", "GCP")
                # Contract / contract-based hiring questions
                elif _re.search(r"contract|contractual|c2h|contract.based|contract to hire|contract role|short term", question):
                    value = af.get("contract_based", "Yes")
                elif "name" in question and "company" not in question:
                    value = cfg.name
                elif "email" in question:
                    value = cfg.email
                elif "phone" in question or "mobile" in question:
                    value = cfg.phone
                elif "location" in question or "city" in question:
                    # "preferred location" = job placement preference; a
                    # bare "location"/"city" question is almost always
                    # asking for actual city of residence, where a
                    # job-preference value like "Remote" isn't a valid city.
                    if "preferred" in question and af.get("preferred_locations"):
                        value = af["preferred_locations"][0]
                    else:
                        value = cfg.location
                elif "age" in question:
                    value = "25"
                elif _re.search(r"current status|employment status|serving notice|resigned", question):
                    value = af.get("current_status", "").split(",")[0].strip() or "Employed"
                elif _re.search(r"how many (organi[sz]ations|companies|employers)|number of (organi[sz]ations|companies|employers)|orgs (worked|till date)", question):
                    value = af.get("total_organizations", "2")
                elif _re.search(r"certificat|certified", question):
                    certs = [c.lower() for c in af.get("certifications", [])]
                    value = "Yes" if any(c and c in question for c in certs) else "No"
                # Open-ended questions (why join, bio, describe yourself, suitability)
                elif _re.search(r"why.*(join|work|company|us|apply|hire)|interest in|describe yourself|about yourself|bio|summary|introduction|profile summary|why.*fit|good fit", question):
                    value = _generate_open_ended_answer(question, af, cfg)
                # Yes/No text questions (AFTER specific data fields) — narrower patterns
                elif _re.search(r"comfortable|willing|ready|agree to|face to face|f2f|onsite|in.person|office|relocat|can you join|ok with|okay with", question):
                    value = "Yes"
                else:
                    # Never leave a question unanswered — most unclassified
                    # chatbot prompts on Naukri are yes/no screening
                    # questions, so default to "Yes" rather than skip.
                    value = "Yes"

                if value:
                    # Click the contenteditable, clear it, type via keyboard
                    ce_el = await page.query_selector('div[contenteditable="true"].textArea, div[contenteditable="true"][data-placeholder]')
                    if ce_el:
                        await ce_el.click()
                        await page.wait_for_timeout(300)
                        # Select all + delete to clear any old text
                        await page.keyboard.press("Control+a")
                        await page.keyboard.press("Meta+a")
                        await page.keyboard.press("Delete")
                        await page.wait_for_timeout(200)
                        # Type the value character by character
                        await page.keyboard.type(str(value), delay=50)
                        await page.wait_for_timeout(500)
                        answered += 1
                        qa_log.append({"question": question[:120], "answer": str(value), "type": "text"})
                        logger.info("Chatbot: typed '%s' for Q: %s", value, question[:40])

            # === 3) STANDARD TEXT INPUTS (fallback for non-chatbot forms) ===
            elif not has_radios:
                await _autofill_fields(page, cfg)

            # === 4) RESUME UPLOAD — only when question asks for it ===
            if cfg.resume_exists and any(w in question for w in ("resume", "cv", "upload", "attach")):
                uploaded = await _upload_resume(page, cfg.resume_path)
                if uploaded:
                    answered += 1

            await page.wait_for_timeout(1000)

            # === 5) CLICK SAVE ===
            # Naukri chatbot Save is: <div class="send"><div class="sendMsg">Save</div></div>
            # The parent div has "disabled" class until input has text.
            # First remove disabled class, then click.
            save_clicked = await page.evaluate("""() => {
                // Remove disabled from send container
                const sendContainer = document.querySelector('div.send, div[class*="sendMsgbtn_container"] div.send');
                if (sendContainer) {
                    sendContainer.classList.remove('disabled');
                }
                // Click the sendMsg div
                const sendMsg = document.querySelector('div.sendMsg');
                if (sendMsg) {
                    sendMsg.click();
                    return 'sendMsg';
                }
                // Also try clicking the container itself
                if (sendContainer) {
                    sendContainer.click();
                    return 'sendContainer';
                }
                // Fallback: any element with text "Save"
                const all = document.querySelectorAll('div, button, a, span');
                for (const el of all) {
                    if (el.offsetParent === null) continue;
                    const t = el.textContent.trim();
                    if (t === 'Save' || t === 'Submit' || t === 'Next') {
                        el.click();
                        return t;
                    }
                }
                return '';
            }""")
            if save_clicked:
                logger.info("Naukri: clicked %s", save_clicked)

        # Final check
        text = (await page.inner_text("body")).lower()
        if "already applied" in text:
            return {"success": True, "confirmation": "Already applied to this job on Naukri", "qa_log": qa_log}
        if "application submitted" in text or "applied successfully" in text:
            return {"success": True, "confirmation": f"Naukri applied (answered {answered} questions)", "qa_log": qa_log}

        return {"success": True, "confirmation": f"Naukri apply completed (answered {answered} questions)", "qa_log": qa_log}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _apply_wellfound(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Apply on Wellfound."""
    try:
        apply_btn = await page.query_selector(
            "button[data-test='apply-button'], button[class*='apply'], a[class*='apply']"
        )
        if not apply_btn:
            return {"success": False, "error": "Apply button not found on Wellfound"}

        await apply_btn.click()
        await page.wait_for_timeout(2000)

        # Cover note
        if cover_note:
            textarea = await page.query_selector(
                "textarea[name*='cover'], textarea[placeholder*='cover'], textarea[data-test='cover-letter']"
            )
            if textarea:
                await textarea.fill(cover_note)

        # Upload resume
        file_input = await page.query_selector("input[type='file']")
        if file_input and cfg.resume_exists:
            await file_input.set_input_files(cfg.resume_path)
            await page.wait_for_timeout(1000)

        # Submit
        submit_btn = await page.query_selector(
            "button[type='submit'], button[data-test='submit-application']"
        )
        if submit_btn:
            await submit_btn.click()
            await page.wait_for_timeout(2000)

        return {"success": True, "confirmation": "Wellfound application submitted"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _apply_indeed(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Apply on Indeed."""
    try:
        apply_btn = await page.query_selector(
            "button#indeedApplyButton, button[class*='apply'], a[class*='apply']"
        )
        if not apply_btn:
            return {"success": False, "error": "Apply button not found on Indeed"}

        await apply_btn.click()
        await page.wait_for_timeout(2000)

        # Fill name
        name_input = await page.query_selector("input[name*='name'], input[id*='name']")
        if name_input:
            current = await name_input.input_value()
            if not current.strip():
                await name_input.fill(cfg.name)

        # Fill email
        email_input = await page.query_selector("input[name*='email'], input[type='email']")
        if email_input:
            current = await email_input.input_value()
            if not current.strip():
                await email_input.fill(cfg.email)

        # Fill phone
        phone_input = await page.query_selector("input[name*='phone'], input[id*='phone']")
        if phone_input:
            current = await phone_input.input_value()
            if not current.strip():
                await phone_input.fill(cfg.phone)

        # Upload resume
        file_input = await page.query_selector("input[type='file']")
        if file_input and cfg.resume_exists:
            await file_input.set_input_files(cfg.resume_path)
            await page.wait_for_timeout(1000)

        # Continue / Submit
        for _ in range(5):
            cont_btn = await page.query_selector(
                "button[id*='continue'], button[class*='continue'], button[type='submit']"
            )
            if cont_btn:
                label = (await cont_btn.inner_text()).lower()
                await cont_btn.click()
                await page.wait_for_timeout(1500)
                if "submit" in label:
                    return {"success": True, "confirmation": "Indeed application submitted"}
            else:
                break

        return {"success": True, "confirmation": "Indeed application flow completed"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _apply_hirist(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Apply on Hirist.tech."""
    try:
        apply_btn = await page.query_selector(
            "button.apply-btn, button[class*='apply'], a.apply-btn"
        )
        if not apply_btn:
            return {"success": False, "error": "Apply button not found on Hirist"}

        await apply_btn.click()
        await page.wait_for_timeout(2000)

        # Upload resume
        file_input = await page.query_selector("input[type='file']")
        if file_input and cfg.resume_exists:
            await file_input.set_input_files(cfg.resume_path)
            await page.wait_for_timeout(1000)

        # Submit
        submit_btn = await page.query_selector("button[type='submit'], button.submit")
        if submit_btn:
            await submit_btn.click()
            await page.wait_for_timeout(2000)

        return {"success": True, "confirmation": "Hirist application submitted"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _apply_glassdoor(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Apply on Glassdoor."""
    try:
        # Glassdoor has "Easy Apply" and "Apply on employer site"
        # Only do Easy Apply
        easy_btn = await page.query_selector(
            "button[data-test='applyButton']:has-text('Easy Apply'), "
            "button[class*='EasyApply'], "
            "button:has-text('Easy Apply')"
        )
        if not easy_btn:
            # Check if it's an external apply
            ext_btn = await page.query_selector(
                "button:has-text('Apply on employer site'), "
                "a:has-text('Apply on employer site')"
            )
            if ext_btn:
                return {"success": False, "error": "External apply (employer site) — skipped"}
            return {"success": False, "error": "No Easy Apply button found on Glassdoor"}

        await easy_btn.click()
        await page.wait_for_timeout(3000)

        # Auto-fill form fields
        filled = await _autofill_fields(page, cfg)

        # Upload resume if prompted
        if cfg.resume_exists:
            await _upload_resume(page, cfg.resume_path)

        # Multi-step: click Next/Submit up to 5 times
        for _ in range(5):
            text = (await page.inner_text("body")).lower()
            if "application submitted" in text or "applied" in text:
                return {"success": True, "confirmation": "Glassdoor Easy Apply submitted"}

            await _autofill_fields(page, cfg)
            if cfg.resume_exists:
                await _upload_resume(page, cfg.resume_path)

            submit = await page.query_selector(
                "button:has-text('Submit'), button:has-text('Next'), "
                "button:has-text('Continue'), button[type='submit']"
            )
            if submit and await submit.is_visible():
                await submit.click()
                await page.wait_for_timeout(2500)
            else:
                break

        return {"success": True, "confirmation": f"Glassdoor apply completed (autofilled {filled} fields)"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _apply_instahyre(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Apply on Instahyre — mostly one-click 'Apply' or 'Interested'."""
    try:
        apply_btn = await page.query_selector(
            "button:has-text('Apply'), button:has-text('Interested'), "
            "button[class*='apply'], a[class*='apply'], "
            "button:has-text('I am interested')"
        )
        if not apply_btn:
            return {"success": False, "error": "Apply button not found on Instahyre"}

        await apply_btn.click()
        await page.wait_for_timeout(3000)

        # Auto-fill any form that appears
        filled = await _autofill_fields(page, cfg)

        # Upload resume if prompted
        if cfg.resume_exists:
            await _upload_resume(page, cfg.resume_path)

        # Click submit/confirm if present
        for _ in range(3):
            submit = await page.query_selector(
                "button:has-text('Submit'), button:has-text('Confirm'), "
                "button:has-text('Apply'), button[type='submit']"
            )
            if submit and await submit.is_visible():
                await submit.click()
                await page.wait_for_timeout(2000)
                await _autofill_fields(page, cfg)
            else:
                break

        text = (await page.inner_text("body")).lower()
        if "applied" in text or "application" in text or "interested" in text:
            return {"success": True, "confirmation": "Instahyre application submitted"}

        return {"success": True, "confirmation": f"Instahyre apply completed (autofilled {filled} fields)"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _apply_cutshort(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Apply on Cutshort — one-click 'Apply' with optional questions."""
    try:
        apply_btn = await page.query_selector(
            "button:has-text('Apply'), button[class*='apply'], "
            "a:has-text('Apply'), button:has-text('I\\'m interested')"
        )
        if not apply_btn:
            return {"success": False, "error": "Apply button not found on Cutshort"}

        await apply_btn.click()
        await page.wait_for_timeout(3000)

        # Auto-fill any form that appears
        filled = await _autofill_fields(page, cfg)

        # Upload resume if prompted
        if cfg.resume_exists:
            await _upload_resume(page, cfg.resume_path)

        # Multi-step: submit through any questionnaire
        for _ in range(5):
            text = (await page.inner_text("body")).lower()
            if "applied" in text or "application submitted" in text:
                return {"success": True, "confirmation": "Cutshort application submitted"}

            await _autofill_fields(page, cfg)
            if cfg.resume_exists:
                await _upload_resume(page, cfg.resume_path)

            submit = await page.query_selector(
                "button:has-text('Submit'), button:has-text('Next'), "
                "button:has-text('Apply'), button[type='submit']"
            )
            if submit and await submit.is_visible():
                await submit.click()
                await page.wait_for_timeout(2500)
            else:
                break

        return {"success": True, "confirmation": f"Cutshort apply completed (autofilled {filled} fields)"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


PLATFORM_APPLYERS = {
    "linkedin": _apply_linkedin,
    "naukri": _apply_naukri,
    "wellfound": _apply_wellfound,
    "indeed": _apply_indeed,
    "hirist": _apply_hirist,
    "glassdoor": _apply_glassdoor,
    "instahyre": _apply_instahyre,
    "cutshort": _apply_cutshort,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def apply_job(
    job_url: str,
    platform: str,
    cover_note: str = "",
    job_title: str = "",
    company: str = "",
    match_score: float = 0.0,
    auto_login: bool = True,
) -> dict[str, Any]:
    """
    Automate a single job application.
    If login is missing and auto_login=True, prompts user with browser window to log in.
    Returns {success, error?, confirmation?, login_required?}.
    """
    platform = platform.lower().strip()
    if platform not in PLATFORM_APPLYERS:
        return {"success": False, "error": f"Unsupported platform: {platform}"}

    cfg = load_config()
    if not cfg.resume_exists:
        return {
            "success": False,
            "error": "Resume file not found. Set 'resume_path' in ~/.job-apply-mcp/config.json",
        }

    # Verify or initiate user login before attempting to apply
    if not _has_saved_login(platform):
        if auto_login:
            logger.info("No saved login for %s. Prompting user to log in now...", platform)
            login_res = await interactive_login(platform)
            if not login_res.get("success"):
                return {
                    "success": False,
                    "login_required": True,
                    "platform": platform,
                    "error": (
                        f"Login to {platform} is required before applying. "
                        f"Browser was opened but login could not be confirmed. "
                        f"Please call login_account(platform='{platform}') and complete sign-in."
                    ),
                    "action_required": f"login_account(platform='{platform}')",
                }
        else:
            return {
                "success": False,
                "login_required": True,
                "platform": platform,
                "error": _missing_login_error(platform),
                "action_required": f"login_account(platform='{platform}')",
            }

    async with async_playwright() as pw:
        # LinkedIn needs persistent browser profile for auth
        if platform == "linkedin":
            profile_dir = str(BROWSER_PROFILES_DIR / "linkedin")
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
            context = await launch_persistent_context(
                pw, profile_dir, headless=False,
                viewport={"width": 1280, "height": 800},
            )
            page = context.pages[0] if context.pages else await context.new_page()
            is_persistent = True

            # Navigate directly to the job posting — confirmed working
            # reliably via live testing. The previous search-page-with-
            # currentJobId redirect was unnecessary and made the Easy
            # Apply button/modal detection unreliable.
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(3000)
        else:
            browser = await launch_browser(pw, headless=False)
            context = await browser.new_context(
                user_agent=get_user_agent(),
                viewport={"width": 1280, "height": 800},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                ignore_https_errors=True,
            )
            await load_cookies(context, platform)
            page = await context.new_page()
            is_persistent = False

            await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(3000)

        if await _detect_captcha(page):
            await context.close() if is_persistent else await browser.close()
            return {
                "success": False,
                "error": (
                    f"CAPTCHA detected on {platform}. "
                    "Please run save_session to log in manually, then retry."
                ),
                "captcha": True,
            }
        if await _looks_logged_out(page, platform):
            await context.close() if is_persistent else await browser.close()
            return {
                "success": False,
                "error": (
                    f"{platform} session appears to be logged out or expired. "
                    f"Run save_session/interactive_login for {platform}, then retry."
                ),
            }

        applyer = PLATFORM_APPLYERS[platform]
        result = await applyer(page, cfg, cover_note)

        # Save cookies after apply (non-LinkedIn only)
        if not is_persistent:
            try:
                await save_cookies_from_context(context, platform)
            except Exception:
                pass

        if is_persistent:
            await context.close()
        else:
            await browser.close()

    # Track in DB
    status = "applied" if result.get("success") else "failed"
    try:
        record_application(
            job_title=job_title or "Unknown",
            company=company or "Unknown",
            platform=platform,
            job_url=job_url,
            status=status,
            confirmation=result.get("confirmation"),
            cover_note=cover_note or None,
            match_score=match_score,
        )
    except Exception as exc:
        logger.warning("Failed to record application: %s", exc)

    return result


async def _apply_in_tab(
    context,
    job_url: str,
    platform: str,
    cfg: AppConfig,
    cover_note: str = "",
) -> dict[str, Any]:
    """Apply to a single job using a new tab in an existing browser context."""
    page = await context.new_page()
    try:
        # Navigate directly to the job posting for all platforms, including
        # LinkedIn — confirmed reliable via live testing (see apply_job).
        await page.goto(job_url, wait_until="domcontentloaded", timeout=25_000)
        await page.wait_for_timeout(2000 if platform != "linkedin" else 3000)

        if await _detect_captcha(page):
            return {"success": False, "error": "CAPTCHA detected", "captcha": True}
        if await _looks_logged_out(page, platform):
            return {
                "success": False,
                "error": (
                    f"{platform} session appears to be logged out or expired. "
                    f"Run save_session/interactive_login for {platform}, then retry."
                ),
            }

        applyer = PLATFORM_APPLYERS.get(platform)
        if not applyer:
            return {"success": False, "error": f"Unsupported platform: {platform}"}

        return await applyer(page, cfg, cover_note)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    finally:
        await page.close()


async def bulk_apply(
    jobs: list[dict[str, Any]],
    max_applications: int = 10,
    dry_run: bool = True,
    max_per_company: int | None = 2,
    company_window_days: int = 1,
) -> dict[str, Any]:
    """
    Apply to multiple jobs using a SINGLE browser session for speed.
    Delay between applications is 5-15 seconds.

    *max_per_company* caps how many roles go to one company (default 2).
    Staffing firms repost near-identical roles under separate URLs, so
    without a cap one recruiter can receive several applications, which
    reads as spam. The count is seeded from applications already recorded
    in the last *company_window_days* days, so the cap holds across
    consecutive batches rather than resetting each run. Pass None to
    disable.
    """
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    # Pre-filter before launching browser
    avoid_companies = [c.strip().lower() for c in load_config().autofill.get("avoid_companies", []) if c.strip()]
    per_company: dict[str, int] = {}
    to_apply: list[dict[str, Any]] = []
    for job in jobs:
        if len(to_apply) >= max_applications:
            break
        url = job.get("apply_url", "")
        platform = job.get("platform", "")
        if not url or not platform:
            skipped.append({**job, "reason": "Missing URL or platform"})
            continue
        if is_already_applied(url):
            skipped.append({**job, "reason": "Already applied"})
            continue
        company = job.get("company", "").strip().lower()
        if company and any(ac in company or company in ac for ac in avoid_companies):
            skipped.append({**job, "reason": "Current/previous employer — excluded"})
            continue
        if max_per_company is not None and company:
            if company not in per_company:
                # Seed from history so the cap spans batches, not just this one.
                per_company[company] = count_recent_applications_for_company(
                    job.get("company", ""), company_window_days
                )
            if per_company[company] >= max_per_company:
                skipped.append({
                    **job,
                    "reason": (
                        f"Company cap reached — already {per_company[company]} "
                        f"application(s) to this company in the last "
                        f"{company_window_days}d (max {max_per_company})"
                    ),
                })
                continue
        # Count before the dry_run branch so a preview reflects the same
        # selection a real run would make.
        if company:
            per_company[company] = per_company.get(company, 0) + 1
        if dry_run:
            applied.append({**job, "dry_run": True})
            continue
        to_apply.append(job)

    if not to_apply or dry_run:
        # Nothing to actually apply to, or dry run already handled above
        pass
    else:
        cfg = load_config()
        if not cfg.resume_exists:
            return {
                "summary": {"error": "Resume not found"},
                "applied": [], "skipped": skipped, "failed": [],
            }

        authenticated_jobs: list[dict[str, Any]] = []
        for job in to_apply:
            platform = job.get("platform", "").lower().strip()
            if not _has_saved_login(platform):
                skipped.append({**job, "reason": _missing_login_error(platform)})
                continue
            authenticated_jobs.append(job)
        to_apply = authenticated_jobs
        if not to_apply:
            return {
                "summary": {
                    "total_processed": len(applied) + len(skipped) + len(failed),
                    "applied": len(applied),
                    "skipped": len(skipped),
                    "failed": len(failed),
                    "dry_run": dry_run,
                },
                "applied": applied,
                "skipped": skipped,
                "failed": failed,
            }

        # Split jobs: LinkedIn (persistent profile) vs others (shared context)
        linkedin_jobs = [j for j in to_apply if j.get("platform") == "linkedin"]
        other_jobs = [j for j in to_apply if j.get("platform") != "linkedin"]

        async def _process_job(context, job, idx, total):
            url = job["apply_url"]
            plat = job["platform"]
            title = job.get("title", "Unknown")
            company = job.get("company", "Unknown")
            logger.info("[%d/%d] Applying: %s @ %s", idx + 1, total, title, company)

            result = await _apply_in_tab(
                context, url, plat, cfg, job.get("cover_note", ""),
            )
            status = "applied" if result.get("success") else "failed"
            try:
                record_application(
                    job_title=title, company=company, platform=plat,
                    job_url=url, status=status,
                    confirmation=result.get("confirmation"),
                    cover_note=job.get("cover_note") or None,
                    match_score=job.get("match_score", 0),
                )
            except Exception:
                pass
            if result.get("success"):
                applied.append({**job, **result})
            else:
                failed.append({**job, **result})

        async with async_playwright() as pw:
            # --- LinkedIn jobs: persistent browser profile ---
            if linkedin_jobs:
                profile_dir = str(BROWSER_PROFILES_DIR / "linkedin")
                Path(profile_dir).mkdir(parents=True, exist_ok=True)
                li_ctx = await launch_persistent_context(
                    pw, profile_dir, headless=False,
                    viewport={"width": 1280, "height": 800},
                )
                for i, job in enumerate(linkedin_jobs):
                    await _process_job(li_ctx, job, i, len(linkedin_jobs))
                    if i < len(linkedin_jobs) - 1:
                        await asyncio.sleep(random.uniform(20, 30))
                await li_ctx.close()

            # --- Other platform jobs: shared context ---
            if other_jobs:
                browser = await launch_browser(pw, headless=False)
                context = await browser.new_context(
                    user_agent=get_user_agent(),
                    viewport={"width": 1280, "height": 800},
                    locale="en-IN",
                    timezone_id="Asia/Kolkata",
                    ignore_https_errors=True,
                )
                platforms_loaded = set()
                for job in other_jobs:
                    p = job.get("platform", "")
                    if p and p not in platforms_loaded:
                        await load_cookies(context, p)
                        platforms_loaded.add(p)

                for i, job in enumerate(other_jobs):
                    await _process_job(context, job, i, len(other_jobs))
                    if i < len(other_jobs) - 1:
                        delay = random.uniform(20, 30)
                        await asyncio.sleep(delay)

                # Save cookies once at end
                for p in platforms_loaded:
                    try:
                        await save_cookies_from_context(context, p)
                    except Exception:
                        pass

                await browser.close()

    return {
        "summary": {
            "total_processed": len(applied) + len(skipped) + len(failed),
            "applied": len(applied),
            "skipped": len(skipped),
            "failed": len(failed),
            "dry_run": dry_run,
        },
        "applied": applied,
        "skipped": skipped,
        "failed": failed,
    }
