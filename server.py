#!/usr/bin/env python3
"""
smart-job-apply-mcp  —  MCP server that automates job searching, filtering,
application submission, and session management across major job portals:
LinkedIn, Naukri, Wellfound, Indeed India, Hirist, Glassdoor, Instahyre, and Cutshort.

Transport: stdio (for Claude Desktop, Cursor, and Antigravity IDE integration)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# Ensure UTF-8 I/O encoding without requiring PYTHONIOENCODING in env
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_config, APP_DIR, CONFIG_PATH
from tools.apply import apply_job, bulk_apply
from tools.contact import extract_contacts
from tools.profile import PROFILE
from tools.search import filter_jobs, search_jobs
from tools.session import (
    SUPPORTED_PLATFORMS,
    check_all_sessions,
    check_platform_session,
    interactive_login,
)
from tools.tracker import get_application_summary

# ---------------------------------------------------------------------------
# Logging (all logs to stderr so stdout is reserved for JSON-RPC MCP frames)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("smart-job-apply-mcp")

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------
server = Server("smart-job-apply-mcp")

# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="check_login_status",
        description=(
            "Verify whether login sessions are active for job platforms before applying. "
            "Returns status for each platform (logged in, missing session, expired). "
            "Always check login status before attempting real job applications."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "platforms": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(SUPPORTED_PLATFORMS),
                    },
                    "description": (
                        "Subset of platforms to verify. If omitted, checks all supported "
                        f"platforms: {list(SUPPORTED_PLATFORMS)}."
                    ),
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="login_account",
        description=(
            "Open a visible browser window for the user to manually log in to a "
            "job platform (via OTP, Google sign-in, or password). The authenticated "
            "session/cookies are automatically captured and saved for automated job applies."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "enum": list(SUPPORTED_PLATFORMS),
                    "description": "Platform to log in to (e.g. 'linkedin', 'naukri', 'indeed').",
                },
                "wait_seconds": {
                    "type": "integer",
                    "default": 180,
                    "description": "Maximum seconds to wait for user to complete login (default 180).",
                },
            },
            "required": ["platform"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="save_session",
        description=(
            "Alias for login_account. Opens a visible browser window so the user "
            "can log in to a job portal and persist authenticated cookies."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "enum": list(SUPPORTED_PLATFORMS),
                    "description": "Platform to log in to.",
                },
                "wait_seconds": {
                    "type": "integer",
                    "default": 180,
                    "description": "Max seconds to wait for login (default 180).",
                },
            },
            "required": ["platform"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="search_jobs",
        description=(
            "Search for jobs across LinkedIn, Naukri, Wellfound, Indeed, Hirist, "
            "Glassdoor, Instahyre, and Cutshort using browser automation. "
            "Returns job listings with match scoring against your candidate profile."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Search keywords. Defaults to target roles in candidate profile if omitted."
                    ),
                },
                "location": {
                    "type": "string",
                    "default": "India",
                    "description": "Location filter for search (e.g. 'Hyderabad', 'Bangalore', 'India', 'Remote').",
                },
                "experience_years": {
                    "type": "integer",
                    "default": 2,
                    "description": "Years of experience filter.",
                },
                "remote": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, filters for remote openings.",
                },
                "platforms": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(SUPPORTED_PLATFORMS),
                    },
                    "description": (
                        "Platforms to search. Defaults to all 8 platforms if omitted."
                    ),
                },
                "days": {
                    "type": "integer",
                    "default": 30,
                    "description": "Only return jobs posted within this many days (e.g. 1 for past 24 hours, 7 for past week).",
                },
                "fetch_jd": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "LinkedIn only: Fetch full job description to re-score against detailed skills."
                    ),
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="filter_jobs",
        description=(
            "Filter and rank jobs by relevance against the candidate profile. "
            "Strictly eliminates over-experienced roles (3+ or 5+ years senior roles) "
            "and blacklisted titles."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "jobs": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of jobs returned by search_jobs.",
                },
                "min_match_score": {
                    "type": "number",
                    "default": 0.65,
                    "description": "Minimum match score threshold (0.0 – 1.0).",
                },
            },
            "required": ["jobs"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="apply_job",
        description=(
            "Apply to a single job via browser automation. Fills standard fields, "
            "uploads resume, and handles platform submission. If the user is not logged in, "
            "prompts for login or reports clear login instructions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "job_url": {
                    "type": "string",
                    "description": "Direct URL to the job posting.",
                },
                "platform": {
                    "type": "string",
                    "enum": list(SUPPORTED_PLATFORMS),
                    "description": "Platform hosting the job.",
                },
                "cover_note": {
                    "type": "string",
                    "default": "",
                    "description": "Optional cover letter or note.",
                },
                "auto_login": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "If true and the platform session is missing, automatically opens "
                        "a browser window asking the user to log in before applying."
                    ),
                },
            },
            "required": ["job_url", "platform"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="bulk_apply",
        description=(
            "Apply to multiple jobs in sequence with rate-limiting (10–25s delay) and safety caps. "
            "Skips already applied jobs and caps applications per company. "
            "Validates user login sessions before starting real submissions. "
            "Use dry_run=true to preview without submitting."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "jobs": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of jobs (from search_jobs or filter_jobs).",
                },
                "max_applications": {
                    "type": "integer",
                    "default": 10,
                    "description": "Maximum applications to submit in this run.",
                },
                "dry_run": {
                    "type": "boolean",
                    "default": True,
                    "description": "Preview mode without submitting real applications.",
                },
                "max_per_company": {
                    "type": "integer",
                    "default": 2,
                    "description": "Max applications allowed per company to avoid looking like spam.",
                },
                "company_window_days": {
                    "type": "integer",
                    "default": 1,
                    "description": "Lookback window for per-company cap.",
                },
            },
            "required": ["jobs"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_application_status",
        description=(
            "Retrieve tracked job applications from the local SQLite database, "
            "including application dates, platform breakdown, and status."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "default": 30,
                    "description": "Number of days of history to inspect (default 30).",
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="extract_job_contacts",
        description=(
            "Extract recruiter emails, career application URLs, and contact names "
            "from raw job descriptions. Used to find recruiter emails for follow-up outreach via Gmail."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Full job description text or posting content.",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="check_profile_config",
        description=(
            "Inspect and validate the local configuration (~/.job-apply-mcp/config.json), "
            "confirming resume PDF existence, candidate details, and autofill fields."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    logger.info("Tool called: %s  args=%s", name, json.dumps(arguments, default=str)[:300])

    try:
        if name == "check_login_status":
            result = check_all_sessions(arguments.get("platforms"))
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name in ("login_account", "save_session"):
            result = await interactive_login(
                platform=arguments["platform"],
                wait_seconds=arguments.get("wait_seconds", 180),
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "search_jobs":
            result = await search_jobs(
                keywords=arguments.get("keywords"),
                location=arguments.get("location", "India"),
                experience_years=arguments.get("experience_years", 2),
                remote=arguments.get("remote", False),
                platforms=arguments.get("platforms"),
                days=arguments.get("days", 30),
                fetch_jd=arguments.get("fetch_jd", True),
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "jobs_found": len(result),
                    "jobs": result,
                }, indent=2),
            )]

        elif name == "filter_jobs":
            result = filter_jobs(
                jobs=arguments["jobs"],
                min_match_score=arguments.get("min_match_score", 0.65),
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "filtered_count": len(result),
                    "jobs": result,
                }, indent=2),
            )]

        elif name == "apply_job":
            result = await apply_job(
                job_url=arguments["job_url"],
                platform=arguments["platform"],
                cover_note=arguments.get("cover_note", ""),
                auto_login=arguments.get("auto_login", True),
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "bulk_apply":
            result = await bulk_apply(
                jobs=arguments["jobs"],
                max_applications=arguments.get("max_applications", 10),
                dry_run=arguments.get("dry_run", True),
                max_per_company=arguments.get("max_per_company", 2),
                company_window_days=arguments.get("company_window_days", 1),
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_application_status":
            result = get_application_summary(
                days=arguments.get("days", 30),
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "extract_job_contacts":
            result = extract_contacts(arguments["text"])
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "check_profile_config":
            cfg = load_config()
            config_status = {
                "config_file": str(CONFIG_PATH),
                "config_exists": CONFIG_PATH.is_file(),
                "name": cfg.name,
                "email": cfg.email,
                "phone": cfg.phone,
                "location": cfg.location,
                "experience_years": cfg.experience_years,
                "resume_path": cfg.resume_path,
                "resume_exists": cfg.resume_exists,
                "autofill_keys_configured": list(cfg.autofill.keys()),
                "status": "ready" if (cfg.resume_exists and cfg.name and cfg.email) else "needs_configuration",
            }
            return [TextContent(type="text", text=json.dumps(config_status, indent=2))]

        else:
            return [TextContent(
                type="text",
                text=json.dumps({"error": f"Unknown tool: {name}"}),
            )]

    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return [TextContent(
            type="text",
            text=json.dumps({"error": str(exc)}),
        )]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    logger.info("Starting smart-job-apply-mcp server (stdio transport)")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
