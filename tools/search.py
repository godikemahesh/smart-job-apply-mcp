"""
search_jobs  — scrape all 5 platforms concurrently with Playwright.
filter_jobs  — rank / filter results against the candidate profile.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any

from playwright.async_api import BrowserContext, Page, async_playwright

from datetime import datetime, timezone

from pathlib import Path

from config import APP_DIR, get_user_agent, load_config
from tools.profile import PROFILE, compute_match_score, should_exclude, title_is_relevant
from tools.session import launch_browser, launch_persistent_context, load_cookies

# Persistent browser profiles for platforms that need full auth (LinkedIn)
BROWSER_PROFILES_DIR = APP_DIR / "browser-profiles"


def _parse_days_ago(text: str) -> int:
    """
    Parse posting age from various formats:
      "3 days ago", "1 week ago", "2 weeks ago", "1 month ago",
      "Few hours ago", "Just now", "Today", "30+ days ago"
    Returns days as int, or -1 if unparseable.
    """
    if not text:
        return -1
    t = text.lower().strip()
    if any(w in t for w in ("just now", "today", "few hours", "hour ago", "hours ago", "moment")):
        return 0
    m = re.search(r"(\d+)\s*day", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*week", t)
    if m:
        return int(m.group(1)) * 7
    m = re.search(r"(\d+)\s*month", t)
    if m:
        return int(m.group(1)) * 30
    if "yesterday" in t:
        return 1
    return -1


def _date_to_days_ago(date_str: str) -> int:
    """Parse an ISO/epoch date string and return days ago. Returns -1 on failure."""
    if not date_str:
        return -1
    try:
        # Naukri uses "DD MMM YYYY" like "14 Apr 2026"
        dt = datetime.strptime(date_str.strip(), "%d %b %Y")
        delta = datetime.now() - dt
        return max(0, delta.days)
    except Exception:
        pass
    try:
        # Try ISO format
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        return max(0, delta.days)
    except Exception:
        pass
    return -1

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class JobResult:
    title: str
    company: str
    location: str
    salary: str
    apply_url: str
    match_score: float
    platform: str
    description: str = ""
    posted_days_ago: int = -1  # -1 means unknown

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Per-platform search URL builders
# ---------------------------------------------------------------------------

def _linkedin_url(keywords: str, location: str, experience: int, days: int = 30) -> str:
    # LinkedIn's f_TPR is a "past N seconds" window — use its own
    # server-side recency filter (the same one the LinkedIn UI's "Past 24
    # hours" pill sets) instead of filtering after the fact, which was
    # unreliable given how few candidates our scraper otherwise sees.
    params = {
        "keywords": keywords,
        "location": location,
        "f_E": "1,2,3",         # internship, entry-level, associate (0-2 years exp)
        "f_AL": "true",         # Easy Apply only
        "sortBy": "DD",         # most recent
        "f_TPR": f"r{max(days, 1) * 86400}",
    }
    return "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)


def _naukri_url(keywords: str, location: str, experience: int, days: int = 30) -> str:
    kw_slug = keywords.lower().replace(" ", "-").replace(",", "-")
    loc_slug = location.lower().replace(" ", "-").replace(",", "")
    exp_min = 0 if experience == 0 else 1
    exp_max = 2
    if loc_slug in ("remote", "all", "india", ""):
        return f"https://www.naukri.com/{kw_slug}-jobs?experience={exp_min}&expmax={exp_max}"
    return f"https://www.naukri.com/{kw_slug}-jobs-in-{loc_slug}?experience={exp_min}&expmax={exp_max}"


def _wellfound_url(keywords: str, location: str, experience: int, days: int = 30) -> str:
    params = {"q": keywords, "location": location}
    return "https://wellfound.com/jobs?" + urllib.parse.urlencode(params)


def _indeed_url(keywords: str, location: str, experience: int, days: int = 30) -> str:
    params = {"q": keywords, "l": location, "fromage": str(min(days, 14))}
    return "https://in.indeed.com/jobs?" + urllib.parse.urlencode(params)


def _hirist_url(keywords: str, location: str, experience: int, days: int = 30) -> str:
    params = {"q": keywords, "loc": location, "exp": str(experience)}
    return "https://www.hirist.tech/jobs?" + urllib.parse.urlencode(params)


def _glassdoor_url(keywords: str, location: str, experience: int, days: int = 30) -> str:
    params = {"q": keywords, "l": location}
    return "https://www.glassdoor.co.in/Job/jobs.htm?" + urllib.parse.urlencode(params)


def _instahyre_url(keywords: str, location: str, experience: int, days: int = 30) -> str:
    params = {"q": keywords, "location": location, "experience": str(experience)}
    return "https://www.instahyre.com/search-jobs/?" + urllib.parse.urlencode(params)


def _cutshort_url(keywords: str, location: str, experience: int, days: int = 30) -> str:
    params = {"q": keywords, "location": location, "experience": f"{experience}-{experience + 2}"}
    return "https://cutshort.io/jobs?" + urllib.parse.urlencode(params)


PLATFORM_BUILDERS: dict[str, Any] = {
    "linkedin": _linkedin_url,
    "naukri": _naukri_url,
    "wellfound": _wellfound_url,
    "indeed": _indeed_url,
    "hirist": _hirist_url,
    "glassdoor": _glassdoor_url,
    "instahyre": _instahyre_url,
    "cutshort": _cutshort_url,
}

# ---------------------------------------------------------------------------
# Per-platform scrapers  (each returns list[JobResult])
# ---------------------------------------------------------------------------

async def _detect_captcha(page: Page) -> bool:
    """Heuristic: look for common CAPTCHA indicators in page text (not HTML src)."""
    try:
        text = (await page.inner_text("body")).lower()
    except Exception:
        text = (await page.content()).lower()
    indicators = [
        "captcha", "recaptcha", "hcaptcha", "cf-challenge",
        "challenge-running", "verify you are human",
    ]
    # Avoid false positives from minified JS or attribute names
    return any(f" {ind}" in f" {text}" or text.startswith(ind) for ind in indicators)


# Cap how many LinkedIn listings get a full-JD fetch per search — each one
# is a real page navigation, so keep this conservative to limit run time
# and stay under LinkedIn's automation-detection radar.
LINKEDIN_JD_FETCH_LIMIT = 15


async def _fetch_linkedin_description(page: Page, url: str) -> str:
    """
    Visit a single LinkedIn job posting and extract the full JD text.

    LinkedIn renders the description under build-hashed CSS classes that
    change often, so class selectors are unreliable. Instead, locate the
    "About the job" marker in the page's plain text and slice from there —
    that heading is stable even when the surrounding markup isn't.
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(2500)
        desc = await page.evaluate("""() => {
            const text = document.body.innerText || '';
            const markers = ['About the job', 'Job description', 'About this job'];
            for (const m of markers) {
                const idx = text.indexOf(m);
                if (idx !== -1) return text.slice(idx, idx + 4000).trim();
            }
            return '';
        }""")
        return desc or ""
    except Exception as exc:
        logger.warning("LinkedIn JD fetch failed for %s: %s", url, exc)
        return ""


async def _scrape_linkedin(page: Page, url: str, fetch_jd: bool = True) -> list[JobResult]:
    """
    Scrape LinkedIn jobs search — uses persistent browser profile for auth.
    URL already has f_AL=true (Easy Apply filter).
    """
    results: list[JobResult] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(5000)

        if await _detect_captcha(page):
            logger.warning("CAPTCHA detected on LinkedIn — skipping")
            return [JobResult(
                title="[CAPTCHA] LinkedIn requires manual verification",
                company="", location="", salary="", apply_url=url,
                match_score=0, platform="linkedin",
            )]

        # Check if redirected to login
        if "login" in page.url:
            return [JobResult(
                title="[LOGIN] LinkedIn session expired — run save_session for linkedin",
                company="", location="", salary="", apply_url=url,
                match_score=0, platform="linkedin",
            )]

        # LinkedIn's job list renders inside its own internally-scrollable
        # panel (build-hashed class name, not stable) rather than the page
        # body, AND virtualizes it — off-screen cards get swapped out of the
        # DOM as you scroll rather than accumulating. So extraction has to
        # happen on every scroll step (accumulating by URL), not once at the
        # end, or everything except the final viewport's worth is lost.
        extract_js = '''() => {
            const jobs = [];
            const cards = document.querySelectorAll(
                'li.jobs-search-results__list-item, ' +
                'div.job-card-container, ' +
                'ul.jobs-search__results-list > li, ' +
                'ul.scaffold-layout__list-container > li'
            );
            for (const card of cards) {
                const linkEl = card.querySelector('a[href*="/jobs/view/"]');
                if (!linkEl) continue;

                const titleEl = card.querySelector(
                    'a.job-card-container__link, a.job-card-list__title--link, ' +
                    'h3, a.job-card-list__title, h3.base-search-card__title'
                );
                const companyEl = card.querySelector(
                    'div.artdeco-entity-lockup__subtitle, ' +
                    'h4, h4.base-search-card__subtitle, ' +
                    'a[data-tracking-control-name*="company"], ' +
                    'span.job-card-container__primary-description'
                );
                const locationEl = card.querySelector(
                    'div.artdeco-entity-lockup__caption li, ' +
                    'div.artdeco-entity-lockup__caption span, ' +
                    'span.job-search-card__location, ' +
                    'li.job-card-container__metadata-item, ' +
                    'span[class*="location"], span.job-card-container__metadata-wrapper'
                );
                const timeEl = card.querySelector('time, span[class*="listed-date"]');

                const title = titleEl ? titleEl.textContent.trim() : '';
                const company = companyEl ? companyEl.textContent.trim() : '';
                const location = locationEl ? locationEl.textContent.trim() : '';
                const href = linkEl.getAttribute('href') || '';
                const posted = timeEl ? timeEl.textContent.trim() : '';

                if (title && title.length > 2) {
                    jobs.push({title, company, location, href, posted});
                }
            }
            return jobs;
        }'''

        accumulated: dict[str, dict] = {}
        stale_rounds = 0
        for _ in range(12):
            batch = await page.evaluate(extract_js)
            added = 0
            for job in batch:
                href = job.get("href", "")
                if href and href not in accumulated:
                    accumulated[href] = job
                    added += 1

            if len(accumulated) >= 40 or stale_rounds >= 2:
                break
            stale_rounds = stale_rounds + 1 if added == 0 else 0

            scrolled = await page.evaluate("""() => {
                const card = document.querySelector('div.job-card-container');
                if (!card) return false;
                let el = card;
                while (el && el !== document.body) {
                    if (el.scrollHeight > el.clientHeight + 10) {
                        el.scrollTop = el.scrollHeight;
                        return true;
                    }
                    el = el.parentElement;
                }
                return false;
            }""")
            if not scrolled:
                break
            await page.wait_for_timeout(1800)

        jobs_data = list(accumulated.values())
        logger.info("LinkedIn: extracted %d unique jobs via JS", len(jobs_data))

        seen_urls = set()
        for job in jobs_data[:40]:
            href = job.get("href", "")
            if href and not href.startswith("http"):
                href = "https://www.linkedin.com" + href

            # Deduplicate by URL
            if href in seen_urls:
                continue
            seen_urls.add(href)

            title = job.get("title", "")
            company = job.get("company", "")
            location = job.get("location", "")
            posted = job.get("posted", "")
            days_ago = _parse_days_ago(posted)

            # Skip jobs older than 30 days
            if days_ago > 30:
                continue

            # Provisional score from title/location only — refined below
            # once the full JD text has been fetched for the best candidates.
            score = compute_match_score(title, "", location)
            results.append(JobResult(
                title=title, company=company, location=location,
                salary="Not listed", apply_url=href or url,
                match_score=score, platform="linkedin",
                posted_days_ago=days_ago,
            ))

        # Optionally fetch the full JD for the strongest title/location
        # candidates and re-score against it, so relevancy reflects actual
        # job content rather than just the title. Each fetch is a real page
        # navigation (~4s), so this dominates search runtime — skip it via
        # fetch_jd=False when the title gate alone is filter enough and
        # speed matters more than score precision.
        results.sort(key=lambda j: j.match_score, reverse=True)
        if fetch_jd:
            for job in results[:LINKEDIN_JD_FETCH_LIMIT]:
                description = await _fetch_linkedin_description(page, job.apply_url)
                if description:
                    job.description = description
                    job.match_score = compute_match_score(job.title, description, job.location)
                await page.wait_for_timeout(random.uniform(1000, 2000))
            results.sort(key=lambda j: j.match_score, reverse=True)
    except Exception as exc:
        logger.error("LinkedIn scrape error: %s", exc)
    return results


async def _scrape_naukri(page: Page, url: str) -> list[JobResult]:
    """
    Scrape Naukri by intercepting its internal /jobapi/v3/search JSON API.
    Fetches multiple pages. Only returns **easy-apply** jobs.
    """
    results: list[JobResult] = []
    all_api_jobs: list[dict] = []
    api_data: dict | None = None

    async def _intercept(route, request):
        nonlocal api_data
        response = await route.fetch()
        if "jobapi/v3/search" in request.url:
            try:
                api_data = json.loads(await response.text())
            except Exception:
                pass
        await route.fulfill(response=response)

    try:
        await page.context.route("**/jobapi/**", _intercept)

        # Fetch up to 5 pages (20 jobs each = 100 jobs max per keyword)
        for page_num in range(1, 6):
            api_data = None
            page_url = url if page_num == 1 else f"{url}&pageNo={page_num}"
            await page.goto(page_url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(4000)

            if not api_data or "jobDetails" not in api_data:
                break  # No more pages

            page_jobs = api_data["jobDetails"]
            if not page_jobs:
                break  # Empty page

            all_api_jobs.extend(page_jobs)
            logger.info("Naukri page %d: %d jobs (total so far: %d)", page_num, len(page_jobs), len(all_api_jobs))

            # Stop if we got fewer than 20 (last page)
            if len(page_jobs) < 20:
                break

        logger.info("Naukri API total: %d jobs across pages", len(all_api_jobs))

        for job in all_api_jobs:
            # --- Filter: only easy/direct apply ---
            if job.get("companyApplyJob", False):
                continue  # skip external "Apply on company site"

            # --- Filter: skip jobs older than 30 days ---
            created = job.get("createdDate", "")
            days_ago = -1
            if isinstance(created, (int, float)) and created > 0:
                # Epoch timestamp (seconds or milliseconds)
                ts = created if created < 1e11 else created / 1000
                delta = datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)
                days_ago = max(0, delta.days)
            elif isinstance(created, str) and created:
                days_ago = _parse_days_ago(created)
                if days_ago == -1:
                    days_ago = _date_to_days_ago(created)
            if days_ago > 30:
                continue

            title = job.get("title", "")
            company = job.get("companyName", "")
            jd_url = job.get("jdURL", "")
            if jd_url and not jd_url.startswith("http"):
                jd_url = "https://www.naukri.com" + jd_url

            # Location from placeholders
            location = ""
            salary = "Not listed"
            for ph in job.get("placeholders", []):
                if ph.get("type") == "location":
                    location = ph.get("label", "")
                elif ph.get("type") == "salary":
                    salary = ph.get("label", "Not listed")

            # Skills for better matching
            skills_str = job.get("tagsAndSkills", "")
            skill_list = [s.strip() for s in skills_str.split(",") if s.strip()] if skills_str else []

            description = job.get("jobDescription", "")

            if title:
                score = compute_match_score(title, description, location, skill_list)
                results.append(JobResult(
                    title=title,
                    company=company,
                    location=location,
                    salary=salary,
                    apply_url=jd_url or url,
                    match_score=score,
                    platform="naukri",
                    description=description[:200],
                    posted_days_ago=days_ago,
                ))
    except Exception as exc:
        logger.error("Naukri scrape error: %s", exc)
    return results


async def _scrape_wellfound(page: Page, url: str) -> list[JobResult]:
    results: list[JobResult] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if await _detect_captcha(page):
            logger.warning("CAPTCHA detected on Wellfound — skipping")
            return [JobResult(
                title="[CAPTCHA] Wellfound requires manual verification",
                company="", location="", salary="", apply_url=url,
                match_score=0, platform="wellfound",
            )]
        await page.wait_for_timeout(2000)

        cards = await page.query_selector_all(
            "div[class*='JobSearchResult'], div[class*='job-listing'], div[data-test='JobListing']"
        )
        for card in cards[:25]:
            title_el = await card.query_selector(
                "a[class*='jobTitle'], h2 a, a[data-test='job-title']"
            )
            company_el = await card.query_selector(
                "a[class*='company'], h2[class*='company'], a[data-test='startup-link']"
            )
            location_el = await card.query_selector(
                "span[class*='location'], span[data-test='location']"
            )
            salary_el = await card.query_selector(
                "span[class*='salary'], span[data-test='compensation']"
            )

            title = (await title_el.inner_text()).strip() if title_el else ""
            company = (await company_el.inner_text()).strip() if company_el else ""
            location = (await location_el.inner_text()).strip() if location_el else ""
            salary = (await salary_el.inner_text()).strip() if salary_el else "Not listed"
            href = await title_el.get_attribute("href") if title_el else ""
            if href and not href.startswith("http"):
                href = "https://wellfound.com" + href

            if title:
                score = compute_match_score(title, "", location)
                results.append(JobResult(
                    title=title, company=company, location=location,
                    salary=salary, apply_url=href or url,
                    match_score=score, platform="wellfound",
                ))
    except Exception as exc:
        logger.error("Wellfound scrape error: %s", exc)
    return results


async def _scrape_indeed(page: Page, url: str) -> list[JobResult]:
    results: list[JobResult] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if await _detect_captcha(page):
            logger.warning("CAPTCHA detected on Indeed — skipping")
            return [JobResult(
                title="[CAPTCHA] Indeed requires manual verification",
                company="", location="", salary="", apply_url=url,
                match_score=0, platform="indeed",
            )]
        await page.wait_for_timeout(2000)

        cards = await page.query_selector_all(
            "div.job_seen_beacon, div.jobsearch-SerpJobCard, td.resultContent"
        )
        for card in cards[:25]:
            title_el = await card.query_selector(
                "h2.jobTitle a, a[data-jk], span[title]"
            )
            company_el = await card.query_selector(
                "span[data-testid='company-name'], span.companyName, span.company"
            )
            location_el = await card.query_selector(
                "div[data-testid='text-location'], div.companyLocation, span.location"
            )
            salary_el = await card.query_selector(
                "div.salary-snippet-container, span.salary-snippet, div.metadata.salary-snippet-container"
            )

            title = (await title_el.inner_text()).strip() if title_el else ""
            company = (await company_el.inner_text()).strip() if company_el else ""
            location = (await location_el.inner_text()).strip() if location_el else ""
            salary = (await salary_el.inner_text()).strip() if salary_el else "Not listed"

            href = ""
            if title_el:
                href = await title_el.get_attribute("href") or ""
            if href and not href.startswith("http"):
                href = "https://in.indeed.com" + href

            if title:
                score = compute_match_score(title, "", location)
                results.append(JobResult(
                    title=title, company=company, location=location,
                    salary=salary, apply_url=href or url,
                    match_score=score, platform="indeed",
                ))
    except Exception as exc:
        logger.error("Indeed scrape error: %s", exc)
    return results


async def _scrape_hirist(page: Page, url: str) -> list[JobResult]:
    results: list[JobResult] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if await _detect_captcha(page):
            logger.warning("CAPTCHA detected on Hirist — skipping")
            return [JobResult(
                title="[CAPTCHA] Hirist requires manual verification",
                company="", location="", salary="", apply_url=url,
                match_score=0, platform="hirist",
            )]
        await page.wait_for_timeout(2000)

        cards = await page.query_selector_all(
            "div.job-card, div[class*='jobCard'], div.job-listing"
        )
        for card in cards[:25]:
            title_el = await card.query_selector("a[class*='title'], h3 a, a.job-title")
            company_el = await card.query_selector("span.company-name, a.company, div.company")
            location_el = await card.query_selector("span.location, div.location")
            salary_el = await card.query_selector("span.salary, div.salary")

            title = (await title_el.inner_text()).strip() if title_el else ""
            company = (await company_el.inner_text()).strip() if company_el else ""
            location = (await location_el.inner_text()).strip() if location_el else ""
            salary = (await salary_el.inner_text()).strip() if salary_el else "Not listed"
            href = await title_el.get_attribute("href") if title_el else ""
            if href and not href.startswith("http"):
                href = "https://www.hirist.tech" + href

            if title:
                score = compute_match_score(title, "", location)
                results.append(JobResult(
                    title=title, company=company, location=location,
                    salary=salary, apply_url=href or url,
                    match_score=score, platform="hirist",
                ))
    except Exception as exc:
        logger.error("Hirist scrape error: %s", exc)
    return results


async def _scrape_glassdoor(page: Page, url: str) -> list[JobResult]:
    """Scrape Glassdoor job listings."""
    results: list[JobResult] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(4000)

        if await _detect_captcha(page):
            logger.warning("CAPTCHA detected on Glassdoor")
            return [JobResult(
                title="[CAPTCHA] Glassdoor requires manual verification",
                company="", location="", salary="", apply_url=url,
                match_score=0, platform="glassdoor",
            )]

        # Glassdoor job cards — try multiple selector patterns
        card_selectors = [
            "li[data-test='jobListing']",
            "li.JobsList_jobListItem__wjTHv",
            "li[class*='JobListItem']",
            "li[class*='react-job-listing']",
            "div[data-test='job-card']",
        ]
        cards = []
        for sel in card_selectors:
            cards = await page.query_selector_all(sel)
            if cards:
                logger.info("Glassdoor: matched %d cards with '%s'", len(cards), sel)
                break

        for card in cards[:25]:
            title_el = await card.query_selector(
                "a[data-test='job-link'], a[class*='JobCard_jobTitle'], "
                "a[class*='jobTitle'], a[href*='/job-listing/']"
            )
            company_el = await card.query_selector(
                "span[class*='EmployerProfile_compactEmployerName'], "
                "div[data-test='emp-name'], span[class*='companyName']"
            )
            location_el = await card.query_selector(
                "div[data-test='emp-location'], span[class*='location'], "
                "div[class*='JobCard_location']"
            )
            salary_el = await card.query_selector(
                "div[data-test='detailSalary'], span[class*='salary'], "
                "div[class*='JobCard_salary']"
            )

            title = (await title_el.inner_text()).strip() if title_el else ""
            company = (await company_el.inner_text()).strip() if company_el else ""
            location = (await location_el.inner_text()).strip() if location_el else ""
            salary = (await salary_el.inner_text()).strip() if salary_el else "Not listed"
            href = await title_el.get_attribute("href") if title_el else ""
            if href and not href.startswith("http"):
                href = "https://www.glassdoor.co.in" + href

            if title:
                score = compute_match_score(title, "", location)
                results.append(JobResult(
                    title=title, company=company, location=location,
                    salary=salary, apply_url=href or url,
                    match_score=score, platform="glassdoor",
                ))

        # Fallback: extract from all job-listing links
        if not results:
            links = await page.query_selector_all("a[href*='/job-listing/']")
            for link in links[:25]:
                href = await link.get_attribute("href") or ""
                text = (await link.inner_text()).strip()
                if text and len(text) > 3:
                    if not href.startswith("http"):
                        href = "https://www.glassdoor.co.in" + href
                    score = compute_match_score(text, "", "India")
                    results.append(JobResult(
                        title=text, company="", location="India",
                        salary="Not listed", apply_url=href or url,
                        match_score=score, platform="glassdoor",
                    ))
    except Exception as exc:
        logger.error("Glassdoor scrape error: %s", exc)
    return results


async def _scrape_instahyre(page: Page, url: str) -> list[JobResult]:
    """Scrape Instahyre job listings."""
    results: list[JobResult] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(4000)

        if await _detect_captcha(page):
            logger.warning("CAPTCHA detected on Instahyre")
            return [JobResult(
                title="[CAPTCHA] Instahyre requires manual verification",
                company="", location="", salary="", apply_url=url,
                match_score=0, platform="instahyre",
            )]

        # Instahyre job cards
        card_selectors = [
            "div.opportunity-card",
            "div[class*='opportunity']",
            "div[class*='job-card']",
            "div[class*='JobCard']",
            "div.card[class*='job']",
        ]
        cards = []
        for sel in card_selectors:
            cards = await page.query_selector_all(sel)
            if cards:
                logger.info("Instahyre: matched %d cards with '%s'", len(cards), sel)
                break

        for card in cards[:25]:
            title_el = await card.query_selector(
                "a[class*='opportunity-title'], h3 a, a[class*='title'], "
                "div[class*='title'] a, a[href*='/opportunity/']"
            )
            company_el = await card.query_selector(
                "div[class*='company-name'], span[class*='company'], "
                "a[class*='company'], div[class*='companyName']"
            )
            location_el = await card.query_selector(
                "div[class*='location'], span[class*='location'], "
                "span[class*='city']"
            )
            salary_el = await card.query_selector(
                "div[class*='salary'], span[class*='salary'], "
                "div[class*='compensation']"
            )

            title = (await title_el.inner_text()).strip() if title_el else ""
            company = (await company_el.inner_text()).strip() if company_el else ""
            location = (await location_el.inner_text()).strip() if location_el else ""
            salary = (await salary_el.inner_text()).strip() if salary_el else "Not listed"
            href = await title_el.get_attribute("href") if title_el else ""
            if href and not href.startswith("http"):
                href = "https://www.instahyre.com" + href

            if title:
                score = compute_match_score(title, "", location)
                results.append(JobResult(
                    title=title, company=company, location=location,
                    salary=salary, apply_url=href or url,
                    match_score=score, platform="instahyre",
                ))

        # Fallback: link extraction
        if not results:
            links = await page.query_selector_all("a[href*='/opportunity/']")
            for link in links[:25]:
                href = await link.get_attribute("href") or ""
                text = (await link.inner_text()).strip()
                if text and len(text) > 3:
                    if not href.startswith("http"):
                        href = "https://www.instahyre.com" + href
                    score = compute_match_score(text, "", "India")
                    results.append(JobResult(
                        title=text, company="", location="India",
                        salary="Not listed", apply_url=href or url,
                        match_score=score, platform="instahyre",
                    ))
    except Exception as exc:
        logger.error("Instahyre scrape error: %s", exc)
    return results


async def _scrape_cutshort(page: Page, url: str) -> list[JobResult]:
    """Scrape Cutshort job listings."""
    results: list[JobResult] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(4000)

        if await _detect_captcha(page):
            logger.warning("CAPTCHA detected on Cutshort")
            return [JobResult(
                title="[CAPTCHA] Cutshort requires manual verification",
                company="", location="", salary="", apply_url=url,
                match_score=0, platform="cutshort",
            )]

        # Cutshort job cards
        card_selectors = [
            "div[class*='job-card']",
            "div[class*='JobCard']",
            "div[class*='jobCard']",
            "a[class*='job-card']",
            "div[class*='opportunity-card']",
            "div[class*='listing-card']",
        ]
        cards = []
        for sel in card_selectors:
            cards = await page.query_selector_all(sel)
            if cards:
                logger.info("Cutshort: matched %d cards with '%s'", len(cards), sel)
                break

        for card in cards[:25]:
            title_el = await card.query_selector(
                "a[class*='title'], h3 a, h2 a, "
                "div[class*='title'] a, a[href*='/job/']"
            )
            company_el = await card.query_selector(
                "div[class*='company'], span[class*='company'], "
                "a[class*='company'], p[class*='company']"
            )
            location_el = await card.query_selector(
                "div[class*='location'], span[class*='location'], "
                "span[class*='city']"
            )
            salary_el = await card.query_selector(
                "div[class*='salary'], span[class*='salary'], "
                "div[class*='compensation'], span[class*='ctc']"
            )
            skills_el = await card.query_selector(
                "div[class*='skills'], div[class*='tags'], "
                "div[class*='tech-stack']"
            )

            title = (await title_el.inner_text()).strip() if title_el else ""
            company = (await company_el.inner_text()).strip() if company_el else ""
            location = (await location_el.inner_text()).strip() if location_el else ""
            salary = (await salary_el.inner_text()).strip() if salary_el else "Not listed"
            skills_text = (await skills_el.inner_text()).strip() if skills_el else ""
            href = await title_el.get_attribute("href") if title_el else ""
            if href and not href.startswith("http"):
                href = "https://cutshort.io" + href

            if title:
                score = compute_match_score(title, skills_text, location)
                results.append(JobResult(
                    title=title, company=company, location=location,
                    salary=salary, apply_url=href or url,
                    match_score=score, platform="cutshort",
                ))

        # Fallback: link extraction
        if not results:
            links = await page.query_selector_all("a[href*='/job/']")
            for link in links[:25]:
                href = await link.get_attribute("href") or ""
                text = (await link.inner_text()).strip()
                if text and len(text) > 3 and "/job/" in href:
                    if not href.startswith("http"):
                        href = "https://cutshort.io" + href
                    score = compute_match_score(text, "", "India")
                    results.append(JobResult(
                        title=text, company="", location="India",
                        salary="Not listed", apply_url=href or url,
                        match_score=score, platform="cutshort",
                    ))
    except Exception as exc:
        logger.error("Cutshort scrape error: %s", exc)
    return results


async def search_linkedin_keywords(
    keywords: list[str],
    location: str = "India",
    experience_years: int = 4,
    days: int = 30,
    fetch_jd: bool = False,
) -> list[dict[str, Any]]:
    """
    Run one LinkedIn search per keyword inside a SINGLE browser session.

    Calling search_jobs() once per keyword launches a new persistent context
    on the same profile directory each time; if the previous one hasn't
    released its lock yet the next launch blocks until timeout. Reusing one
    context avoids that entirely and is much faster. Results are deduped by
    URL with LinkedIn's tracking query string stripped.
    """
    profile_dir = str(BROWSER_PROFILES_DIR / "linkedin")
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    merged: dict[str, JobResult] = {}
    async with async_playwright() as pw:
        ctx = await launch_persistent_context(
            pw, profile_dir, headless=False, viewport={"width": 1280, "height": 800},
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            for kw in keywords:
                url = _linkedin_url(kw, location, experience_years, days)
                try:
                    results = await _scrape_linkedin(page, url, fetch_jd)
                except Exception as exc:
                    logger.error("LinkedIn search failed for %r: %s", kw, exc)
                    continue
                added = 0
                for job in results:
                    clean = job.apply_url.split("?")[0]
                    if clean and clean not in merged:
                        job.apply_url = clean
                        merged[clean] = job
                        added += 1
                logger.info("LinkedIn %r: %d results, +%d new (total %d)", kw, len(results), added, len(merged))
        finally:
            await ctx.close()

    jobs = sorted(merged.values(), key=lambda j: j.match_score, reverse=True)
    return [j.to_dict() for j in jobs]


PLATFORM_SCRAPERS = {
    "linkedin": _scrape_linkedin,
    "naukri": _scrape_naukri,
    "wellfound": _scrape_wellfound,
    "indeed": _scrape_indeed,
    "hirist": _scrape_hirist,
    "glassdoor": _scrape_glassdoor,
    "instahyre": _scrape_instahyre,
    "cutshort": _scrape_cutshort,
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def search_jobs(
    keywords: list[str] | None = None,
    location: str = "India",
    experience_years: int = 3,
    remote: bool = False,
    platforms: list[str] | None = None,
    days: int = 30,
    fetch_jd: bool = True,
) -> list[dict[str, Any]]:
    """
    Search job platforms concurrently and return merged results.
    If *platforms* is given, only search those (e.g. ["naukri"]).
    *days* is passed to each platform's own recency filter where supported
    (e.g. LinkedIn's "Past N hours/days" search filter) rather than only
    filtering client-side after scraping.
    """
    if not keywords:
        keywords = list(PROFILE.default_search_keywords)

    kw_string = ", ".join(keywords)
    if remote:
        kw_string += " remote"

    # Determine which platforms to search
    active_platforms = (
        [p for p in platforms if p in PLATFORM_SCRAPERS]
        if platforms
        else list(PLATFORM_SCRAPERS.keys())
    )

    # Separate LinkedIn (needs persistent profile) from other platforms
    linkedin_platforms = [p for p in active_platforms if p == "linkedin"]
    other_platforms = [p for p in active_platforms if p != "linkedin"]

    platform_results: list = []

    async with async_playwright() as pw:
        # --- LinkedIn: use persistent browser profile ---
        if linkedin_platforms:
            profile_dir = str(BROWSER_PROFILES_DIR / "linkedin")
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
            try:
                li_ctx = await launch_persistent_context(
                    pw, profile_dir, headless=False,
                    viewport={"width": 1280, "height": 800},
                )
                li_page = li_ctx.pages[0] if li_ctx.pages else await li_ctx.new_page()
                li_url = PLATFORM_BUILDERS["linkedin"](kw_string, location, experience_years, days)
                li_results = await _scrape_linkedin(li_page, li_url, fetch_jd)
                platform_results.append(li_results)
                await li_ctx.close()
            except Exception as exc:
                logger.error("LinkedIn scrape failed: %s", exc)
                platform_results.append(exc)

        # --- Other platforms: shared browser context ---
        if other_platforms:
            browser = await launch_browser(pw, headless=False)
            context = await browser.new_context(
                user_agent=get_user_agent(),
                viewport={"width": 1280, "height": 800},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                ignore_https_errors=True,
            )

            # Load any saved sessions
            for platform in other_platforms:
                await load_cookies(context, platform)

            # Build URLs
            urls: dict[str, str] = {}
            for platform in other_platforms:
                urls[platform] = PLATFORM_BUILDERS[platform](kw_string, location, experience_years, days)

            # Scrape concurrently — one page per platform
            async def _run(platform: str) -> list[JobResult]:
                page = await context.new_page()
                try:
                    scraper = PLATFORM_SCRAPERS[platform]
                    return await scraper(page, urls[platform])
                finally:
                    await page.close()

            tasks = [_run(p) for p in other_platforms]
            other_results = await asyncio.gather(*tasks, return_exceptions=True)
            platform_results.extend(other_results)

            await browser.close()

    # Merge
    all_jobs: list[JobResult] = []
    for res in platform_results:
        if isinstance(res, list):
            all_jobs.extend(res)
        else:
            logger.error("Platform scrape failed: %s", res)

    # Sort by match score descending
    all_jobs.sort(key=lambda j: j.match_score, reverse=True)
    return [j.to_dict() for j in all_jobs]


def filter_jobs(
    jobs: list[dict[str, Any]],
    min_match_score: float = 0.7,
    max_days_old: int = 30,
) -> list[dict[str, Any]]:
    """
    Filter and rank jobs from search_jobs output.
    - Removes jobs below min_match_score
    - Excludes jobs in avoid categories
    - Excludes jobs posted more than max_days_old days ago
    - Returns top 20
    """
    avoid_companies = [c.strip().lower() for c in load_config().autofill.get("avoid_companies", []) if c.strip()]

    filtered: list[dict[str, Any]] = []
    for job in jobs:
        if not title_is_relevant(job.get("title", "")):
            continue
        if job.get("match_score", 0) < min_match_score:
            continue
        if should_exclude(job.get("title", ""), job.get("description", "")):
            continue
        # Never apply to the candidate's current/previous employers
        company = job.get("company", "").strip().lower()
        if company and any(ac in company or company in ac for ac in avoid_companies):
            continue
        # Skip jobs older than max_days_old (allow -1 = unknown through)
        days = job.get("posted_days_ago", -1)
        if days > max_days_old:
            continue
        filtered.append(job)

    filtered.sort(key=lambda j: j.get("match_score", 0), reverse=True)
    return filtered[:50]
