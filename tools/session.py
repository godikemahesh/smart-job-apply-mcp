"""
Browser session / cookie management for each job platform.

Cookies are stored as JSON files under ~/.job-apply-mcp/sessions/<platform>.json
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, async_playwright

from config import APP_DIR, SESSIONS_DIR, ensure_dirs

logger = logging.getLogger(__name__)

# LinkedIn is driven through a persistent Chrome profile rather than saved
# cookies (it ties its session to browser fingerprint/local storage, so a
# cookie-only replay gets logged out). Search and apply both launch this
# same profile directory, so the login has to happen inside it.
BROWSER_PROFILES_DIR = APP_DIR / "browser-profiles"
PERSISTENT_PROFILE_PLATFORMS = {"linkedin"}

PLATFORM_LOGIN_URLS: dict[str, str] = {
    "linkedin": "https://www.linkedin.com/login",
    "naukri": "https://www.naukri.com/mnjuser/login",
    "wellfound": "https://wellfound.com/login",
    "indeed": "https://secure.indeed.com/auth",
    "hirist": "https://www.hirist.tech/login",
    "glassdoor": "https://www.glassdoor.co.in/profile/login_input.htm",
    "instahyre": "https://www.instahyre.com/login/",
    "cutshort": "https://cutshort.io/login",
}

SUPPORTED_PLATFORMS = tuple(PLATFORM_LOGIN_URLS.keys())

LOGIN_CHROME_ARGS = [
    "--start-maximized",
    "--window-position=0,0",
    "--disable-blink-features=AutomationControlled",
]
LOGIN_CHROME_IGNORE_DEFAULT_ARGS = ["--enable-automation"]


def _cookie_path(platform: str) -> Path:
    return SESSIONS_DIR / f"{platform}.json"


async def launch_browser(pw, headless: bool = False, **kwargs):
    """Launch browser, automatically falling back to Chromium if Firefox is missing host dependencies."""
    try:
        return await pw.chromium.launch(headless=headless, **kwargs)
    except Exception as exc:
        if "missing dependencies" in str(exc).lower() or "msvcp140" in str(exc).lower():
            logger.warning("Firefox missing host dependencies. Falling back to Chromium.")
            return await pw.chromium.launch(headless=headless, **kwargs)
        raise


async def launch_persistent_context(pw, user_data_dir: str, headless: bool = False, **kwargs):
    """Launch persistent context, automatically falling back to Chromium if Firefox is missing host dependencies."""
    try:
        return await pw.chromium.launch_persistent_context(user_data_dir, headless=headless, **kwargs)
    except Exception as exc:
        if "missing dependencies" in str(exc).lower() or "msvcp140" in str(exc).lower():
            logger.warning("Firefox missing host dependencies. Falling back to Chromium.")
            return await pw.chromium.launch_persistent_context(user_data_dir, headless=headless, **kwargs)
        raise


async def launch_login_browser(pw, **kwargs):
    """Launch an installed Chrome window for manual account sign-in.

    Google may reject Playwright's bundled Firefox/Chromium identity during
    OAuth sign-in. Using the installed Chrome channel gives the user a normal
    Chrome login surface while Playwright still records the resulting session.
    """
    try:
        return await pw.chromium.launch(
            channel="chrome",
            headless=False,
            args=LOGIN_CHROME_ARGS,
            ignore_default_args=LOGIN_CHROME_IGNORE_DEFAULT_ARGS,
            **kwargs,
        )
    except Exception as exc:
        logger.warning("Installed Chrome is unavailable for login: %s", exc)
        return await launch_browser(pw, headless=False, **kwargs)


async def launch_login_persistent_context(pw, user_data_dir: str, **kwargs):
    """Launch a persistent installed-Chrome profile for manual sign-in."""
    try:
        return await pw.chromium.launch_persistent_context(
            user_data_dir,
            channel="chrome",
            headless=False,
            args=LOGIN_CHROME_ARGS,
            ignore_default_args=LOGIN_CHROME_IGNORE_DEFAULT_ARGS,
            **kwargs,
        )
    except Exception as exc:
        logger.warning("Installed Chrome is unavailable for login: %s", exc)
        return await launch_persistent_context(pw, user_data_dir, headless=False, **kwargs)


async def _interactive_login_persistent(platform: str, url: str, wait_seconds: int = 180) -> dict[str, Any]:
    """
    Log in inside the persistent browser profile that search/apply reuse.

    Saving cookies would be useless here: the search and apply flows launch
    this profile directory directly and never load the cookie jar, so the
    login must be performed in the profile itself.
    """
    import asyncio
    profile_dir = BROWSER_PROFILES_DIR / platform
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        context = await launch_login_persistent_context(
            pw, str(profile_dir),
            no_viewport=True,
            locale="en-IN", timezone_id="Asia/Kolkata",
        )
        google_blocked = False
        logged_in = False
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            logger.info(
                "Browser opened for %s. Log in (password or OTP) — the session "
                "is stored in the profile itself. Polling for login completion (up to %ds)...",
                platform,
                wait_seconds,
            )
            
            # Intelligently poll for logged-in state instead of rigid blocking sleep
            start_time = asyncio.get_event_loop().time()
            while (asyncio.get_event_loop().time() - start_time) < wait_seconds:
                await page.wait_for_timeout(2000)
                google_blocked = await _google_security_blocked(context)
                if google_blocked:
                    break
                
                curr_url = page.url.lower()
                # Check for successful post-login redirection
                if "/feed" in curr_url or ("/mynetwork" in curr_url) or ("/in/" in curr_url) or (
                    "linkedin.com" in curr_url
                    and "/login" not in curr_url
                    and "checkpoint" not in curr_url
                    and "uas/consumer-login" not in curr_url
                    and "signup" not in curr_url
                ):
                    logged_in = True
                    logger.info("Detected successful login on %s!", platform)
                    await page.wait_for_timeout(3000)
                    break
        finally:
            await context.close()

    return {
        "success": logged_in and not google_blocked,
        "platform": platform,
        "storage": f"persistent browser profile: {profile_dir}",
        "looks_logged_in": logged_in,
        **(
            {
                "error": (
                    "Google rejected this automated OAuth sign-in. "
                    "Use the platform email/password login or a normal Chrome window."
                )
            }
            if google_blocked
            else {}
        ),
        "note": (
            "Session saved to persistent browser profile."
            if logged_in
            else "Login was not completed within the time limit. Run login_account again when ready."
        ),
    }


def has_session(platform: str) -> bool:
    """Return True if saved cookies or persistent profile exists for the platform."""
    platform = platform.lower().strip()
    if platform in PERSISTENT_PROFILE_PLATFORMS:
        profile_dir = BROWSER_PROFILES_DIR / platform
        return profile_dir.is_dir() and any(profile_dir.iterdir())
    return _cookie_path(platform).is_file()


def check_platform_session(platform: str) -> dict[str, Any]:
    """Check if a platform has a valid saved session or persistent profile."""
    platform = platform.lower().strip()
    if platform not in SUPPORTED_PLATFORMS:
        return {
            "platform": platform,
            "supported": False,
            "logged_in": False,
            "error": f"Unsupported platform. Supported: {', '.join(SUPPORTED_PLATFORMS)}",
        }

    if platform in PERSISTENT_PROFILE_PLATFORMS:
        profile_dir = BROWSER_PROFILES_DIR / platform
        exists = profile_dir.is_dir() and any(profile_dir.iterdir())
        return {
            "platform": platform,
            "supported": True,
            "logged_in": exists,
            "session_type": "persistent_browser_profile",
            "path": str(profile_dir),
            "hint": "Session active and ready" if exists else f"Run login_account(platform='{platform}') to authenticate",
        }

    cookie_file = _cookie_path(platform)
    if cookie_file.is_file():
        try:
            cookies = json.loads(cookie_file.read_text())
            has_cookies = len(cookies) > 0
            return {
                "platform": platform,
                "supported": True,
                "logged_in": has_cookies,
                "cookies_count": len(cookies),
                "session_type": "cookie_jar",
                "path": str(cookie_file),
                "hint": "Session active and ready" if has_cookies else f"Empty cookie jar. Run login_account(platform='{platform}')",
            }
        except Exception:
            return {
                "platform": platform,
                "supported": True,
                "logged_in": False,
                "error": "Corrupted cookie jar. Re-login required.",
                "hint": f"Run login_account(platform='{platform}')",
            }
    return {
        "platform": platform,
        "supported": True,
        "logged_in": False,
        "session_type": "cookie_jar",
        "hint": f"No session found. Run login_account(platform='{platform}')",
    }


def check_all_sessions(platforms: list[str] | None = None) -> dict[str, Any]:
    """Check session statuses across all supported platforms or specified subset."""
    target_platforms = [p.lower().strip() for p in platforms] if platforms else list(SUPPORTED_PLATFORMS)
    statuses = {p: check_platform_session(p) for p in target_platforms}
    logged_in_count = sum(1 for s in statuses.values() if s.get("logged_in"))
    return {
        "total_checked": len(statuses),
        "logged_in_count": logged_in_count,
        "ready_to_apply": logged_in_count > 0,
        "platforms": statuses,
    }


async def load_cookies(context: BrowserContext, platform: str) -> bool:
    """
    Load saved cookies into a Playwright BrowserContext.
    Returns True if cookies were loaded.
    """
    path = _cookie_path(platform)
    if not path.is_file():
        logger.warning("No saved session for %s", platform)
        return False
    cookies = json.loads(path.read_text())
    await context.add_cookies(cookies)
    logger.info("Loaded %d cookies for %s", len(cookies), platform)
    return True


async def save_cookies_from_context(
    context: BrowserContext, platform: str
) -> int:
    """Persist current cookies from a BrowserContext to disk."""
    ensure_dirs()
    cookies = await context.cookies()
    path = _cookie_path(platform)
    path.write_text(json.dumps(cookies, indent=2))
    logger.info("Saved %d cookies for %s", len(cookies), platform)
    return len(cookies)


async def _google_security_blocked(context: BrowserContext) -> bool:
    """Detect Google's unsupported-browser OAuth page in any open tab."""
    marker = "this browser or app may not be secure"
    for page in context.pages:
        try:
            text = (await page.locator("body").inner_text(timeout=2_000)).lower()
        except Exception:
            continue
        if marker in text:
            return True
    return False


async def interactive_login(platform: str, wait_seconds: int = 180) -> dict[str, Any]:
    """
    Open a **visible** browser window so the user can log in manually.
    Monitors for successful login, saves cookies, and returns the status.
    """
    import asyncio
    platform = platform.lower().strip()
    if platform not in PLATFORM_LOGIN_URLS:
        return {
            "success": False,
            "error": f"Unsupported platform '{platform}'. Choose from: {', '.join(SUPPORTED_PLATFORMS)}",
        }

    url = PLATFORM_LOGIN_URLS[platform]
    ensure_dirs()

    if platform in PERSISTENT_PROFILE_PLATFORMS:
        return await _interactive_login_persistent(platform, url, wait_seconds=wait_seconds)

    async with async_playwright() as pw:
        browser = await launch_login_browser(pw)
        context = await browser.new_context(
            no_viewport=True,
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            ignore_https_errors=True,
        )

        # Load existing cookies if any (lets user resume partial sessions)
        await load_cookies(context, platform)

        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        logger.info(
            "Browser opened for %s login. Please log in manually. "
            "Polling for login completion (up to %ds)...",
            platform,
            wait_seconds,
        )

        logged_in = False
        google_blocked = False
        start_time = asyncio.get_event_loop().time()

        while (asyncio.get_event_loop().time() - start_time) < wait_seconds:
            await page.wait_for_timeout(2000)
            google_blocked = await _google_security_blocked(context)
            if google_blocked:
                break

            curr_url = page.url.lower()
            # Platform-specific logged-in checks
            if platform == "naukri" and (
                "/mnjuser/homepage" in curr_url
                or "/mydrawer" in curr_url
                or ("naukri.com" in curr_url and "/login" not in curr_url and "/mnjuser" in curr_url)
            ):
                logged_in = True
                break
            elif platform == "indeed" and (
                "indeed.com" in curr_url
                and "secure.indeed.com/auth" not in curr_url
                and "/account/login" not in curr_url
            ):
                logged_in = True
                break
            elif platform == "glassdoor" and (
                "glassdoor" in curr_url
                and "login" not in curr_url
                and "signin" not in curr_url
            ):
                logged_in = True
                break
            elif platform == "wellfound" and (
                "wellfound.com" in curr_url and "/login" not in curr_url
            ):
                logged_in = True
                break
            elif platform == "hirist" and (
                "hirist.tech" in curr_url and "/login" not in curr_url
            ):
                logged_in = True
                break
            elif platform in ("instahyre", "cutshort") and ("/login" not in curr_url):
                logged_in = True
                break

        # Save cookies
        count = 0
        if not google_blocked:
            count = await save_cookies_from_context(context, platform)
            if count > 0:
                logged_in = True

        await browser.close()

    return {
        "success": logged_in and not google_blocked,
        "platform": platform,
        "cookies_saved": count,
        "session_path": str(_cookie_path(platform)),
        **(
            {
                "error": (
                    "Google rejected this automated OAuth sign-in. "
                    "Use the platform email/password login instead of Continue with Google."
                )
            }
            if google_blocked
            else {}
        ),
        "note": (
            f"Successfully authenticated and saved {count} cookies for {platform}."
            if logged_in
            else f"Login not detected within {wait_seconds}s. Run login_account again when ready."
        ),
    }


# Friendly alias
login_account = interactive_login

