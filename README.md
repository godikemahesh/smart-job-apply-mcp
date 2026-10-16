# smart-job-apply-mcp & gmail-mcp

> **Autonomous AI Job Hunting, Form Auto-Fill, and Recruiter Outreach Suite across 8 Job Portals powered by Model Context Protocol (MCP), Playwright, and Google Antigravity IDE.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-green.svg)](https://modelcontextprotocol.io/)
[![Playwright](https://img.shields.io/badge/Playwright-Browser%20Automation-orange.svg)](https://playwright.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

> ### 🚀 Autonomous Job Hunting — Sit Back, Relax, and Let Your Agent Work
>
> **Imagine you sit back and relax while your autonomous AI agent takes over your entire job hunt:**
> - **Searches jobs across all platforms**: **LinkedIn**, **Naukri**, **Wellfound**, **Indeed India**, **Glassdoor**, **Hirist**, **Instahyre**, and **Cutshort**.
> - **Applies directly to Easy-Apply roles** in seconds with intelligent auto-fill and tailored resume uploads.
> - **Navigates redirects to company career pages and external ATS systems** (Greenhouse, Lever, Workday) to seamlessly fill and submit external applications.
> - **Extracts recruiter contacts and sends personalized outreach emails** with your resume attached to secure screening calls.
>
> **smart-job-apply-mcp does everything a human job seeker does to discover, apply, and network — completely automated, 100x faster, and 24/7.**

---

## Overview: The Tri-MCP Autonomous Job Hunt Architecture

Applying to hundreds of high-probability job openings manually is exhausting. **smart-job-apply-mcp** solves this by providing a complete, agent-ready Model Context Protocol (MCP) toolset.

When orchestrated inside **Google Antigravity IDE** alongside `@playwright/mcp` and `gmail-mcp`, it forms a tri-engine autonomous job-hunting machine:

```
                      +------------------------------------------+
                      |          Google Antigravity IDE          |
                      |       (Autonomous Multi-Agent AI)       |
                      +--------------------+---------------------+
                                           |
         +---------------------------------+---------------------------------+
         |                                 |                                 |
         v                                 v                                 v
+-----------------------+       +---------------------+       +-----------------------+
|  smart-job-apply-mcp  |       |   @playwright/mcp   |       |       gmail-mcp       |
| • 8 Job Portals       |       | • External ATS Portals|     | • 17 Gmail Tools      |
| • Relevance Matching  |       | • Greenhouse/Lever  |       | • Recruiter Outreach  |
| • Resume Auto-Upload  |       | • Workday Multi-step|       | • Cold Pitching       |
| • Honest Smart Fill   |       | • Custom SPAs       |       | • Interview Tracking  |
| • SQLite Tracker      |       | • Live Fallbacks    |       | • Inbox Triage        |
+-----------------------+       +---------------------+       +-----------------------+
         |                                 |                                 |
         +---------------------------------+---------------------------------+
                                           |
                                           v
                       Real Submissions, Confirmations & Interviews
```

1. **`smart-job-apply-mcp` (Core Automation)**:
   - High-throughput search and relevance filtering across **8 portals**: **LinkedIn**, **Naukri**, **Wellfound**, **Indeed India**, **Hirist**, **Glassdoor**, **Instahyre**, and **Cutshort**.
   - **Strict Experience Gate**: Eliminates over-qualified roles (e.g. 3+ to 5+ year senior/lead/architect roles) for early-career candidates, focusing 100% of effort on high-probability fits.
   - **Smart Form Auto-Fill**: Intelligently handles text inputs, radio buttons, dropdowns, notice period, CTC, and resume uploads.
   - **Honest Answers**: Only claims skills you actually possess in your config, answering `0` for unlisted skills rather than inheriting total experience.
   - **Persistent Session & Login Guard**: Prevents failed guest submissions by checking and prompting for authentications up front.
   - **Duplicate Shield**: Local SQLite database tracks all applied job URLs across batches and platforms.

2. **`@playwright/mcp` (External ATS Navigator)**:
   - Handles jobs that redirect away from job boards to third-party enterprise ATS systems (Greenhouse, Lever, Workday, SmartRecruiters, Taleo).
   - Allows agents to inspect custom forms, complete multi-step applications, and navigate back seamlessly.

3. **`gmail-mcp` (Recruiter Outreach & Response Triage)**:
   - Extracts hiring manager and recruiter emails from job descriptions.
   - Sends customized, professional outreach emails pitching your skills and attaching your resume.
   - Monitors inbox for interview invites, screening calls, and confirmation receipts.

---

## Proven Case Study: 450+ Autonomous Applications in a Single Day

In real-world production runs conducted with Google Antigravity, this suite executed end-to-end campaigns:

- **450+ real applications submitted in one single day** across major platforms:
  - **LinkedIn**: 195+ applications
  - **Naukri**: 180+ applications
  - **Indeed**: 50+ applications
  - **Glassdoor & Company Career Portals**: 25+ applications
- **Strict Role Relevance**: Zero senior roles misapplied. Every role matched the candidate's exact domain (AI/ML/Computer Vision) and experience tier (0–2 years).
- **Recruiter Outreach**: Candidate contacts extracted from job descriptions were contacted via `gmail-mcp`, leading directly to scheduled screening calls with resumes attached.

---

## 8 Supported Platforms

| Platform | Search | Easy Apply | Redirects | Session Type |
|:---|:---:|:---:|:---:|:---|
| **LinkedIn** | Yes | Yes | Yes (via Playwright) | Persistent Chrome Profile |
| **Naukri** | Yes | Yes | Yes | Authenticated Cookie Jar |
| **Indeed India** | Yes | Yes | Yes | Authenticated Cookie Jar |
| **Wellfound** | Yes | Yes | Yes | Authenticated Cookie Jar |
| **Glassdoor** | Yes | Yes | Yes | Authenticated Cookie Jar |
| **Hirist** | Yes | Yes | Yes | Authenticated Cookie Jar |
| **Instahyre** | Yes | Yes | Yes | Authenticated Cookie Jar |
| **Cutshort** | Yes | Yes | Yes | Authenticated Cookie Jar |

---

## User-Friendly Login Management

Job portals reject unauthenticated applications submitted as guests. `smart-job-apply-mcp` makes authentication seamless:

### 1. `check_login_status`
Run at the start of a session or before applying to see which platforms are ready:
```json
{
  "total_checked": 8,
  "logged_in_count": 5,
  "ready_to_apply": true,
  "platforms": {
    "linkedin": { "logged_in": true, "session_type": "persistent_browser_profile", "hint": "Session active and ready" },
    "naukri":   { "logged_in": true, "cookies_count": 147, "hint": "Session active and ready" },
    "hirist":   { "logged_in": false, "hint": "No session found. Run login_account(platform='hirist')" }
  }
}
```

### 2. `login_account(platform)` (or `save_session`)
Opens a visible browser window directly to the platform's login page.
- Log in manually via OTP, Password, or Google Sign-In.
- The server **reactively detects successful login** as soon as the page redirects and saves your cookies / persistent profile immediately (no rigid waiting!).

### 3. Automatic Pre-Apply Check
If you or an AI agent calls `apply_job` or `bulk_apply` on a platform without a saved session:
- If `auto_login=True` (default for `apply_job`), the browser automatically launches asking you to log in before submitting.
- In `bulk_apply`, unauthenticated platforms are reported up front with exact instructions on how to log in.

---

## Tool Reference

### `smart-job-apply-mcp` Tools

| Tool | Parameters | Description |
|:---|:---|:---|
| `check_login_status` | `platforms` (optional array) | Check which job platforms have active saved logins. |
| `login_account` | `platform` (string), `wait_seconds` (int, default 180) | Open visible browser to log in; automatically captures and saves session cookies/profile. |
| `save_session` | `platform` (string) | Backward-compatible alias for `login_account`. |
| `search_jobs` | `keywords`, `location`, `experience_years`, `remote`, `platforms`, `days`, `fetch_jd` | Search jobs across platforms using browser automation with match scoring. |
| `filter_jobs` | `jobs` (array), `min_match_score` (float, default 0.65) | Filter jobs against candidate profile and strictly discard senior/avoid roles. |
| `apply_job` | `job_url`, `platform`, `cover_note`, `auto_login` (bool, default True) | Apply to a single job with autofill, resume upload, and login verification. |
| `bulk_apply` | `jobs`, `max_applications` (int), `dry_run` (bool), `max_per_company` (int) | Batch apply with rate-limiting, safety delays, and duplicate avoidance. |
| `get_application_status` | `days` (int, default 30) | Query application history from local SQLite database. |
| `extract_job_contacts` | `text` (string) | Extract recruiter email addresses, career URLs, and contact names from job postings. |
| `check_profile_config` | *(none)* | Validate `~/.smart-job-apply-mcp/config.json` and resume PDF path. |

### `gmail-mcp` Tools

| Tool | Description |
|:---|:---|
| `send_email` | Send a new email with safety checks (`confirm_new_recipient`) and draft fallback for `draftsOnly` accounts. |
| `reply_email` | Reply to an existing thread/message, auto-setting headers and referencing original message ID. |
| `forward_email` | Forward a message with full history and attachments to new recipients. |
| `create_draft` | Create a draft email in your Gmail mailbox without sending. |
| `list_accounts` | List configured email accounts, their provider types, and authentication status. |
| `authenticate` | Add a new Gmail account via standard local browser OAuth2. |
| `reauth` | Re-run OAuth2 when access tokens expire or scopes change. |
| `remove_account` | Remove a configured account and its credentials from `accounts.json`. |
| `search_emails` | Search Gmail messages using full Gmail query syntax (e.g. `is:unread from:hr@`). |
| `read_email` | Read full content, headers, and text/HTML body of an email. |
| `read_thread` | Read all messages within a conversation thread. |
| `inbox_summary` | Get total and unread counts, plus categorized triage (recruiting, alerts, newsletters). |
| `modify_email` | Add or remove labels/flags on a message. |
| `batch_modify_emails` | Add or remove labels across multiple messages in a single batch. |
| `trash_emails` | Move one or more messages to Gmail Trash. |
| `bulk_trash` | Search and batch-trash messages matching a query (with `dry_run=true` support). |
| `bulk_modify` | Search and batch-modify labels for messages matching a query. |

---

## Installation & Setup

### 1. Prerequisites
- **Python 3.10+** (Python 3.10, 3.11, or 3.12)
- **Node.js 18+** (for `@playwright/mcp`)
- **Google Chrome** installed on your system (recommended for Google OAuth sign-in)

### 2. Clone Repository & Install Dependencies
```bash
git clone https://github.com/godikemahesh/smart-job-apply-mcp.git
cd smart-job-apply-mcp

# Install dependencies & register global CLI commands
pip install -e .

# Install Playwright browser binaries
python -m playwright install chromium firefox
```

### 3. Configure Your Profile
Copy the template configuration to `~/.smart-job-apply-mcp/config.json`:

```bash
# Windows PowerShell
New-Item -ItemType Directory -Force -Path "$HOME\.smart-job-apply-mcp"
Copy-Item "config.template.json" -Destination "$HOME\.smart-job-apply-mcp\config.json"

# macOS / Linux
mkdir -p ~/.smart-job-apply-mcp
cp config.template.json ~/.smart-job-apply-mcp/config.json
```

Edit `~/.smart-job-apply-mcp/config.json` with your real resume path, contact details, and technical skill years:
```json
{
  "resume_path": "C:\\Users\\YourUser\\Documents\\resume.pdf",
  "name": "Alex Doe",
  "email": "alex.doe@example.com",
  "phone": "+91-9876543210",
  "location": "Bengaluru, India",
  "experience_years": 2,
  "autofill": {
    "gender": "Male",
    "notice_period": "Immediate Joiner",
    "current_ctc": "6",
    "expected_ctc": "12",
    "total_experience": "2",
    "primary_cloud": "AWS",
    "experience": {
      "python": "2",
      "machine learning": "2",
      "pytorch": "2",
      "docker": "2",
      "aws": "2"
    }
  }
}
```

### 4. Configure Gmail OAuth (for `gmail-mcp`)
1. Create a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the **Gmail API** in APIs & Services.
3. Configure the **OAuth Consent Screen** (User Type: External, add your email as Test User).
4. Create an **OAuth Client ID** (Application Type: **Desktop app**).
5. Download the JSON credential file and save it as:
   `~/.gmail-mcp/oauth-keys.json` (or `~/.mailbox-mcp/oauth-keys.json`).
6. Run `authenticate` or start the server — the browser will open once to grant permission.

---

## MCP Server Configuration

Configuring the servers is effortless. **`"env"` is NOT required** because all module imports, paths, and UTF-8 encodings are automatically handled by the server engines.

### 🌟 Recommended: Direct CLI Commands (Zero Path Edits)

After running `pip install -e .`, both `smart-job-apply-mcp` and `gmail-mcp` are registered globally in your environment. You can paste this exact snippet directly into **Google Antigravity IDE** (`~/.gemini/config/mcp_config.json`), **Claude Desktop** (`%APPDATA%\Claude\claude_desktop_config.json`), or **Cursor / Windsurf**:

```json
{
  "mcpServers": {
    "smart-job-apply-mcp": {
      "command": "smart-job-apply-mcp"
    },
    "gmail-mcp": {
      "command": "gmail-mcp"
    },
    "playwright": {
      "command": "npx",
      "args": [
        "-y",
        "@playwright/mcp@latest"
      ]
    }
  }
}
```

> **Why no `"env"` block?**
> Standard setups often clutter configs with `PYTHONPATH` or `PYTHONIOENCODING`. `smart-job-apply-mcp` dynamically discovers its own root directory and configures UTF-8 I/O in Python code, eliminating all unnecessary boilerplate.

---

### Alternative: Direct Python Script (Without `pip install -e .`)

If you prefer pointing directly to the script files without installing them globally:

```json
{
  "mcpServers": {
    "smart-job-apply-mcp": {
      "command": "python",
      "args": ["C:/path/to/smart-job-apply-mcp/server.py"]
    },
    "gmail-mcp": {
      "command": "python",
      "args": ["C:/path/to/smart-job-apply-mcp/gmail_server.py"]
    },
    "playwright": {
      "command": "npx",
      "args": ["-y", "@playwright/mcp@latest"]
    }
  }
}
```
*(Tip: Forward slashes `/` work reliably across Windows, macOS, and Linux without escape issues.)*

---

## Ways to Use

### 1. Autonomous AI Agent in Antigravity or Claude
Once configured, you can talk naturally to your AI assistant:
> *"Check my login status across all platforms, then search for fresh (<3 days) AI/ML Engineer jobs in Hyderabad and Remote. Filter out any roles requiring 3+ or 5+ years of experience. Present the top 10 matches, and if any description lists a recruiter email, prepare a cold outreach draft in Gmail with my resume."*

### 2. Standalone Interactive Terminal Runner (`run.py`)
No MCP client required! Run directly from the terminal:
```bash
python run.py
```
Menu options:
1. **Check login status** (verify sessions for all 8 portals)
2. **Login to a platform** (opens headful browser to log in)
3. **Search & Apply for jobs** (interactive keyword search, preview, and apply)
4. **View application status history** (query database)
5. **Search only** (browse and evaluate matches)

### 3. Autonomous Groq CLI Agent (`agent.py`)
Runs an autonomous conversational agent from the terminal using Groq LLM (with multi-key rotation and 0-cost inference):
```bash
cp .env.example .env
# Add your GROQ_API_KEYS in .env
python agent.py
```

---

## Troubleshooting & FAQ

#### 1. Why was my application skipped with "Company cap reached"?
`bulk_apply` enforces a per-company cap (default 2 applications per company) seeded from your history over the past `company_window_days`. This prevents spamming a single employer with multiple applications.

#### 2. How are CAPTCHAs handled?
When a CAPTCHA or Cloudflare challenge is encountered, the automation halts and logs a notice. In headful mode, you can solve the challenge directly in the browser; the tool then resumes execution.

#### 3. How do I refresh an expired LinkedIn session?
LinkedIn ties sessions to a persistent Chrome profile. Run `login_account(platform='linkedin')` or select option 2 in `python run.py` to open the profile, sign in, and complete any 2FA challenge.

---

## License

MIT License. Free for personal and commercial use. Contributions and PRs welcome!
