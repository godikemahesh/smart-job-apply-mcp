#!/usr/bin/env python3
"""
Smart Job Apply AI Agent powered by Groq with Multi-API Key Fallback.

Features:
- Seamless multi-key Groq rotation: Provide comma-separated API keys in GROQ_API_KEYS.
  If a key hits rate limits (TPM/RPM 429), quota limits, or auth errors, it automatically
  fails over to the next key without crashing.
- Full integration with Smart Job Apply MCP tools:
  * search_jobs
  * filter_jobs
  * apply_job
  * bulk_apply
  * get_application_status
  * check_login_status
  * login_account / save_session
  * check_profile_config
- Interactive chat REPL and single-command CLI modes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Ensure UTF-8 output on Windows consoles to prevent UnicodeEncodeError
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Load environment variables
from dotenv import load_dotenv

# Try loading .env then .env.local
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT / ".env.local")

try:
    from groq import (
        APIConnectionError,
        APIError,
        AuthenticationError,
        Groq,
        InternalServerError,
        RateLimitError,
    )
except ImportError:
    print("Error: 'groq' package is not installed. Run: pip install groq")
    sys.exit(1)

from config import APP_DIR, CONFIG_PATH, load_config
from tools.apply import apply_job, bulk_apply
from tools.profile import PROFILE
from tools.search import filter_jobs, search_jobs
from tools.session import SUPPORTED_PLATFORMS, interactive_login
from tools.tracker import get_application_summary

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
# Silence verbose third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("groq").setLevel(logging.WARNING)
logger = logging.getLogger("job-agent")


# ---------------------------------------------------------------------------
# Multi-Key Groq Rotator
# ---------------------------------------------------------------------------

class GroqKeyManager:
    """Manages multiple Groq API keys with automatic failover on rate limits / errors."""

    def __init__(self, raw_keys: str | list[str], model: str = "llama-3.3-70b-versatile"):
        if isinstance(raw_keys, str):
            # Split comma or newline separated keys
            keys = [k.strip() for k in raw_keys.replace("\n", ",").split(",") if k.strip()]
        else:
            keys = [k.strip() for k in raw_keys if k and k.strip()]

        # Filter out placeholders
        self.api_keys: list[str] = [
            k for k in keys if not k.startswith("gsk_your_") and len(k) > 10
        ]
        self.model = model
        self.current_index = 0
        self.clients: list[Groq] = [Groq(api_key=k) for k in self.api_keys]

    @property
    def has_keys(self) -> bool:
        return len(self.api_keys) > 0

    @property
    def key_count(self) -> int:
        return len(self.api_keys)

    def get_current_client(self) -> Groq:
        if not self.has_keys:
            raise ValueError(
                "No valid Groq API keys configured. "
                "Set GROQ_API_KEYS=key1,key2,... in your .env file."
            )
        return self.clients[self.current_index]

    def mask_key(self, idx: int) -> str:
        if 0 <= idx < len(self.api_keys):
            k = self.api_keys[idx]
            if len(k) <= 12:
                return f"{k[:3]}...{k[-3:]}"
            return f"{k[:6]}...{k[-4:]}"
        return "none"

    def rotate_key(self, reason: str = "") -> None:
        if len(self.api_keys) <= 1:
            logger.warning("Only 1 Groq API key available; cannot rotate. Reason: %s", reason)
            return

        old_idx = self.current_index
        self.current_index = (self.current_index + 1) % len(self.api_keys)
        logger.warning(
            "⚡ [Groq Key Rotator] Switched from Key #%d (%s) to Key #%d (%s). Reason: %s",
            old_idx + 1,
            self.mask_key(old_idx),
            self.current_index + 1,
            self.mask_key(self.current_index),
            reason,
        )

    def create_completion(self, **kwargs) -> Any:
        """Call chat.completions.create with automatic key rotation on failover errors."""
        max_attempts = max(len(self.api_keys) * 2, 2)
        attempts = 0
        last_error = None

        while attempts < max_attempts:
            attempts += 1
            client = self.get_current_client()
            current_key_display = f"Key #{self.current_index + 1} ({self.mask_key(self.current_index)})"

            try:
                response = client.chat.completions.create(
                    model=self.model,
                    **kwargs,
                )
                return response

            except RateLimitError as e:
                last_error = e
                err_msg = str(e).splitlines()[0] if str(e) else "Rate limit reached (429)"
                print(f"\n[bold yellow]⚠️ Groq {current_key_display} hit Rate Limit:[/bold yellow] {err_msg}")
                self.rotate_key("RateLimitError (429)")
                time.sleep(1.0)

            except AuthenticationError as e:
                last_error = e
                print(f"\n[bold red]❌ Groq {current_key_display} Authentication Error:[/bold red] {e}")
                self.rotate_key("AuthenticationError (401)")

            except (InternalServerError, APIConnectionError) as e:
                last_error = e
                print(f"\n[bold yellow]⚠️ Groq {current_key_display} Server/Connection Error:[/bold yellow] {e}")
                self.rotate_key("Server/Connection Error")
                time.sleep(1.5)

            except APIError as e:
                last_error = e
                # Check for 429 status inside generic APIError
                if getattr(e, "status_code", None) == 429:
                    print(f"\n[bold yellow]⚠️ Groq {current_key_display} 429 Too Many Requests[/bold yellow]")
                    self.rotate_key("Status 429")
                    time.sleep(1.0)
                else:
                    print(f"\n[bold red]❌ Groq API Error on {current_key_display}:[/bold red] {e}")
                    self.rotate_key(f"APIError: {e}")

            except Exception as e:
                last_error = e
                logger.exception("Unexpected error calling Groq on %s", current_key_display)
                self.rotate_key(f"Unexpected error: {e}")

        raise RuntimeError(
            f"All Groq API keys ({len(self.api_keys)}) failed after {attempts} attempts. "
            f"Last error: {last_error}"
        )


# ---------------------------------------------------------------------------
# Tool Declarations for Groq (Function Calling)
# ---------------------------------------------------------------------------

GROQ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_jobs",
            "description": (
                "Search for jobs across major platforms (naukri, linkedin, wellfound, indeed, "
                "hirist, glassdoor, instahyre, cutshort). Returns ranked job listings."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Target job title keywords to search (e.g. ['AI Engineer', 'Computer Vision Engineer', 'Machine Learning Engineer', 'GenAI Engineer', 'LLM Engineer']). "
                            "Must strictly match the candidate's specialization (AI/ML/Computer Vision). "
                            "Do NOT search for unrelated roles like Java, Frontend, or Full Stack Developer."
                        ),
                    },
                    "location": {
                        "type": "string",
                        "default": "India",
                        "description": "Target job location (default: 'India').",
                    },
                    "experience_years": {
                        "type": "integer",
                        "default": 3,
                        "description": "Target years of experience.",
                    },
                    "remote": {
                        "type": "boolean",
                        "default": False,
                        "description": "Filter for remote positions.",
                    },
                    "platforms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            f"Platforms to search. Options: {', '.join(SUPPORTED_PLATFORMS)}. "
                            "Defaults to all supported if omitted."
                        ),
                    },
                    "days": {
                        "type": "integer",
                        "default": 14,
                        "description": "Only return jobs posted within this many days (e.g. 1 for past 24h, 7 for past week).",
                    },
                    "fetch_jd": {
                        "type": "boolean",
                        "default": False,
                        "description": "For LinkedIn: fetch full job description for deeper scoring (slower).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "filter_jobs",
            "description": (
                "Filter and rank jobs against target roles and profile skills. "
                "Can take a raw jobs list or use 'use_latest_search' to filter the most recently searched jobs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "jobs": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Array of jobs returned by search_jobs. Optional if use_latest_search is true.",
                    },
                    "use_latest_search": {
                        "type": "boolean",
                        "default": True,
                        "description": "If true, automatically uses the jobs found in the most recent search.",
                    },
                    "min_match_score": {
                        "type": "number",
                        "default": 0.3,
                        "description": "Minimum match relevance score between 0.0 and 1.0.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_job",
            "description": (
                "Apply to a single job posting via browser automation. "
                "Uses candidate profile and resume to complete the application."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_url": {
                        "type": "string",
                        "description": "Direct URL of the job posting.",
                    },
                    "platform": {
                        "type": "string",
                        "description": f"Platform name: {', '.join(SUPPORTED_PLATFORMS)}.",
                    },
                    "cover_note": {
                        "type": "string",
                        "default": "",
                        "description": "Optional custom cover message.",
                    },
                },
                "required": ["job_url", "platform"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bulk_apply",
            "description": (
                "Apply to multiple jobs in batch with safety limits and rate limiting. "
                "NOTE: Keep dry_run=true for previewing. Only set dry_run=false when the user explicitly confirms applying."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "jobs": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of jobs to apply to. If omitted, uses latest filtered jobs.",
                    },
                    "use_latest_filtered": {
                        "type": "boolean",
                        "default": True,
                        "description": "If true, uses the latest filtered jobs.",
                    },
                    "max_applications": {
                        "type": "integer",
                        "default": 5,
                        "description": "Maximum number of applications to submit in this run.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": True,
                        "description": "PREVIEW MODE if true. Set false ONLY when user approved real submission.",
                    },
                    "max_per_company": {
                        "type": "integer",
                        "default": 2,
                        "description": "Cap applications to any single company to prevent recruiter spam.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_application_status",
            "description": "Retrieve tracked job applications from the local SQLite database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "default": 7,
                        "description": "How many days back to inspect.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_session",
            "description": (
                "Open a visible browser window allowing the user to manually log in to a platform "
                "(e.g., Naukri, LinkedIn) and store persistent session cookies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "description": f"Platform name: {', '.join(SUPPORTED_PLATFORMS)}.",
                    },
                },
                "required": ["platform"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_profile_config",
            "description": "Check candidate profile config, resume path existence, and configured autofill credentials.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Job Agent Implementation
# ---------------------------------------------------------------------------

class JobAgent:
    """Autonomous Job Application Agent powered by Groq and Playwright MCP tools."""

    @classmethod
    def build_system_prompt(cls) -> str:
        cfg = load_config()
        name = cfg.name or "Candidate"
        exp = cfg.experience_years or 3
        loc = cfg.location or "India"
        pref_locs = cfg.autofill.get("preferred_locations", [loc, "Remote"])
        target_roles = list(PROFILE.target_roles)
        return f"""You are an intelligent Job Application Assistant operating on top of Smart Job Apply MCP.
You have direct browser automation tools to search, filter, and apply to job opportunities across 8 job portals (Naukri, LinkedIn, Wellfound, Indeed, Hirist, Glassdoor, Instahyre, Cutshort).

CANDIDATE PROFILE & TARGET SPECIALIZATION:
- Candidate Name: {name}
- Experience: {exp} years
- Base Location: {loc}
- Preferred Locations: {pref_locs}
- Specialization: {PROFILE.title}
- Target Roles: {target_roles}
- Default Keywords: {list(PROFILE.default_search_keywords)}

CORE PRINCIPLES & GUIDELINES:
1. SAFE SUBMISSION POLICY:
   - Applying to jobs submits real applications that cannot be undone.
   - Always prefer searching, filtering, and presenting a ranked shortlist (with title, company, match score, location) to the user first.
   - Always run `bulk_apply` with `dry_run=true` first unless the user explicitly commands: "apply directly", "submit for real", or gives explicit go-ahead.
2. PROFILE AWARENESS:
   - Check profile details using `check_profile_config` when needed to verify candidate parameters.
3. CONCISE & ACTIONABLE RESPONSES:
   - Present shortlisted jobs in clean, readable tables or numbered lists.
   - Show match scores, company names, ages (e.g. '2d ago'), and platforms clearly.
   - Prompt the user with next actions: e.g., "Would you like me to bulk apply to the top 5 with dry_run=false?".
4. PLATFORM NUANCES:
   - Naukri has high volume; LinkedIn has smaller pools and Easy Apply flows.
   - If a CAPTCHA or missing session occurs, advise the user to run `save_session` or `login_account`.
"""

    def __init__(self, key_manager: GroqKeyManager):
        self.key_manager = key_manager
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.build_system_prompt()}
        ]
        # Memory caches to avoid sending massive payloads through LLM tokens
        self.latest_search_jobs: list[dict[str, Any]] = []
        self.latest_filtered_jobs: list[dict[str, Any]] = []

    def _compact_job(self, j: dict[str, Any]) -> dict[str, Any]:
        """Compact job representation to conserve Groq context tokens."""
        return {
            "title": j.get("title", ""),
            "company": j.get("company", ""),
            "location": j.get("location", ""),
            "platform": j.get("platform", ""),
            "match_score": round(float(j.get("match_score", 0)), 2),
            "apply_url": j.get("apply_url", ""),
        }

    async def execute_tool(self, name: str, args: dict[str, Any]) -> Any:
        """Execute a local MCP tool and return the serializable result."""
        logger.info("Executing tool: %s with args: %s", name, json.dumps(args, default=str)[:200])

        if name == "search_jobs":
            cfg = load_config()
            keywords = args.get("keywords")
            # If keywords passed are generic or unrelated (like Java, Frontend, Developer), sanitize them
            if not keywords or any(bad in " ".join(keywords).lower() for bad in ["java", "frontend", "full stack", "backend engineer", "developer"]):
                keywords = list(PROFILE.default_search_keywords)

            location = args.get("location") or cfg.location or "India"
            experience_years = args.get("experience_years", cfg.experience_years)
            remote = args.get("remote", False)
            platforms = args.get("platforms")
            days = args.get("days", 14)
            fetch_jd = args.get("fetch_jd", False)

            jobs = await search_jobs(
                keywords=keywords,
                location=location,
                experience_years=experience_years,
                remote=remote,
                platforms=platforms,
                days=days,
                fetch_jd=fetch_jd,
            )
            self.latest_search_jobs = jobs
            compacted = [self._compact_job(j) for j in jobs[:5]]
            return {
                "total_found": len(jobs),
                "sample_preview": compacted,
                "note": f"Found {len(jobs)} jobs. Cached in memory. Next step: use filter_jobs.",
            }

        elif name == "filter_jobs":
            jobs_to_filter = args.get("jobs")
            if not jobs_to_filter or args.get("use_latest_search", True):
                jobs_to_filter = self.latest_search_jobs

            if not jobs_to_filter:
                return {"error": "No jobs to filter. Please run search_jobs first."}

            min_score = args.get("min_match_score", 0.3)
            filtered = filter_jobs(jobs_to_filter, min_match_score=min_score)
            self.latest_filtered_jobs = filtered
            compacted = [self._compact_job(j) for j in filtered[:5]]
            return {
                "filtered_count": len(filtered),
                "top_jobs": compacted,
            }

        elif name == "apply_job":
            job_url = args["job_url"]
            platform = args["platform"]
            cover_note = args.get("cover_note", "")
            return await apply_job(job_url=job_url, platform=platform, cover_note=cover_note)

        elif name == "bulk_apply":
            jobs_to_apply = args.get("jobs")
            if not jobs_to_apply or args.get("use_latest_filtered", True):
                jobs_to_apply = self.latest_filtered_jobs or self.latest_search_jobs

            if not jobs_to_apply:
                return {"error": "No jobs available to apply. Please search and filter jobs first."}

            max_apps = args.get("max_applications", 5)
            dry_run = args.get("dry_run", True)
            max_per_co = args.get("max_per_company", 2)

            result = await bulk_apply(
                jobs=jobs_to_apply,
                max_applications=max_apps,
                dry_run=dry_run,
                max_per_company=max_per_co,
            )
            return result

        elif name == "get_application_status":
            days = args.get("days", 7)
            return get_application_summary(days=days)

        elif name == "save_session":
            platform = args["platform"]
            return await interactive_login(platform=platform)

        elif name == "check_profile_config":
            cfg = load_config()
            return {
                "config_file_path": str(CONFIG_PATH),
                "config_exists": CONFIG_PATH.is_file(),
                "candidate_name": cfg.name or "(not set)",
                "specialization": PROFILE.title,
                "target_roles": list(PROFILE.target_roles),
                "default_search_keywords": list(PROFILE.default_search_keywords),
                "email": cfg.email or "(not set)",
                "phone": cfg.phone or "(not set)",
                "location": cfg.location,
                "preferred_locations": cfg.autofill.get("preferred_locations", list(PROFILE.preferred_locations)),
                "experience_years": cfg.experience_years,
                "resume_path": cfg.resume_path or "(not set)",
                "resume_file_exists": cfg.resume_exists,
                "core_skills": list(PROFILE.skills[:20]),
                "bio": cfg.autofill.get("bio", ""),
            }

        else:
            return {"error": f"Unknown tool: {name}"}

    async def run_turn(self, user_input: str, on_progress: Callable[[str], None] | None = None) -> str:
        """Run an autonomous multi-step reasoning & tool calling turn."""
        self.messages.append({"role": "user", "content": user_input})

        max_steps = 10
        step = 0

        while step < max_steps:
            step += 1

            # Request Groq completion using the key manager (auto-rotates on error)
            response = self.key_manager.create_completion(
                messages=self.messages,
                tools=GROQ_TOOLS,
                tool_choice="auto",
                temperature=0.3,
            )

            choice = response.choices[0]
            message = choice.message
            tool_calls = getattr(message, "tool_calls", None)

            # If model produced text content, record it
            if message.content:
                # We save assistant message state
                pass

            if not tool_calls:
                # Finished responding
                self.messages.append({"role": "assistant", "content": message.content or ""})
                return message.content or ""

            # Model wants to call tools
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
            self.messages.append(assistant_msg)

            # Process each tool call
            for tc in tool_calls:
                t_name = tc.function.name
                t_id = tc.id
                raw_args = tc.function.arguments or "{}"

                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}

                if on_progress:
                    on_progress(f"🔧 [cyan]Running tool: {t_name}[/cyan] ({json.dumps(args, default=str)[:100]}...)")

                try:
                    res = await self.execute_tool(t_name, args)
                    res_str = json.dumps(res, default=str)
                except Exception as exc:
                    logger.exception("Error executing tool %s", t_name)
                    res_str = json.dumps({"error": str(exc)})

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": t_id,
                    "content": res_str,
                })

        return "Reached maximum steps for this turn. Please let me know how you'd like to proceed!"


# ---------------------------------------------------------------------------
# CLI & Terminal Interface
# ---------------------------------------------------------------------------

def print_welcome_banner(key_mgr: GroqKeyManager):
    print("=" * 65)
    print("       🤖 JOB APPLY AI AGENT — POWERED BY GROQ & PLAYWRIGHT")
    print("=" * 65)
    print(f"  • Groq Provider   : {key_mgr.key_count} API key(s) loaded (Auto-Fallback ON)")
    print(f"  • Model           : {key_mgr.model}")
    print(f"  • Platforms       : {', '.join(SUPPORTED_PLATFORMS)}")

    cfg = load_config()
    resume_status = (
        f"✅ Loaded ({cfg.resume_path})" if cfg.resume_exists
        else f"⚠️ NOT FOUND (Set in {CONFIG_PATH})"
    )
    print(f"  • Resume File     : {resume_status}")
    print(f"  • Candidate Profile: {cfg.name or 'Not set in config.json'}")
    print("=" * 65)
    print("  Commands: type your query in natural language.")
    print("  Examples:")
    print("    - 'Search Naukri for DevOps jobs posted in the last 3 days'")
    print("    - 'Filter the jobs for high relevance and show me the top 5'")
    print("    - 'Preview applications for the top 3 with dry_run'")
    print("    - 'Check my application history for the last 7 days'")
    print("    - 'status' / 'config' / 'exit'")
    print("=" * 65)
    print()


async def interactive_repl(agent: JobAgent):
    print_welcome_banner(agent.key_manager)

    while True:
        try:
            user_input = input("User ❯ ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting. Good luck with your job search!")
            break

        if not user_input:
            continue

        cmd = user_input.lower()
        if cmd in ("exit", "quit", "q"):
            print("Goodbye!")
            break
        elif cmd in ("status", "/status"):
            summary = get_application_summary(days=7)
            print(f"\n[Status Summary (last 7d)] Total: {summary['total']}")
            print(json.dumps(summary["by_status"], indent=2))
            continue
        elif cmd in ("config", "/config"):
            cfg = load_config()
            print(f"\n[Config Status: {CONFIG_PATH}]")
            print(f"  Name: {cfg.name}")
            print(f"  Email: {cfg.email}")
            print(f"  Resume: {'EXISTS' if cfg.resume_exists else 'MISSING'} -> {cfg.resume_path}")
            continue

        print("\nThinking and executing actions...", flush=True)

        def show_progress(msg: str):
            print(f"  {msg}", flush=True)

        try:
            response = await agent.run_turn(user_input, on_progress=show_progress)
            print(f"\nAgent ❯\n{response}\n")
        except Exception as e:
            print(f"\n[Error]: {e}\n")


async def main():
    # Load Groq API keys from environment
    raw_keys = os.getenv("GROQ_API_KEYS", os.getenv("GROQ_API_KEY", ""))
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    key_manager = GroqKeyManager(raw_keys=raw_keys, model=model)

    if not key_manager.has_keys:
        print("\n" + "!" * 65)
        print(" ERROR: No Groq API keys found in environment or .env file!")
        print("!" * 65)
        print(" Please create a .env file in this directory:")
        print("   c:\\Users\\USER\\Downloads\\job-apply-mcp\\.env\n")
        print(" Add your Groq API key(s) separated by commas:")
        print("   GROQ_API_KEYS=gsk_key1,gsk_key2,gsk_key3\n")
        print(" You can get free Groq API keys at: https://console.groq.com/keys")
        print("!" * 65 + "\n")
        sys.exit(1)

    agent = JobAgent(key_manager=key_manager)

    # Check if a prompt was provided via command line arguments
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
        print(f"Running command: {query}\n")
        response = await agent.run_turn(query, on_progress=lambda m: print(f"  {m}"))
        print(f"\nAgent: {response}\n")
    else:
        await interactive_repl(agent)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
