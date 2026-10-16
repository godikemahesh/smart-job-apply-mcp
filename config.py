"""
Configuration loader for smart-job-apply-mcp.

Reads and writes ~/.smart-job-apply-mcp/config.json (with fallback to ~/.job-apply-mcp) which stores:
  - resume_path        : absolute path to the resume PDF
  - name               : candidate full name
  - email              : candidate email
  - phone              : candidate phone number
  - location           : candidate location string
  - experience_years   : integer
  - credentials        : per-platform login details (optional)
"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Primary app directory with backward compatibility
NEW_APP_DIR = Path.home() / ".smart-job-apply-mcp"
OLD_APP_DIR = Path.home() / ".job-apply-mcp"
APP_DIR = NEW_APP_DIR if (NEW_APP_DIR.exists() or not OLD_APP_DIR.exists()) else OLD_APP_DIR
CONFIG_PATH = APP_DIR / "config.json"
SESSIONS_DIR = APP_DIR / "sessions"
DB_PATH = APP_DIR / "applications.db"

DEFAULT_CONFIG: dict[str, Any] = {
    "resume_path": "",
    "name": "",
    "email": "",
    "phone": "",
    "location": "India",
    "experience_years": 3,
    "credentials": {
        "linkedin": {"email": "", "password": ""},
        "naukri": {"email": "", "password": ""},
        "wellfound": {"email": "", "password": ""},
        "indeed": {"email": "", "password": ""},
        "hirist": {"email": "", "password": ""},
    },
}


@dataclass
class AppConfig:
    resume_path: str = ""
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = "India"
    experience_years: int = 3
    credentials: dict[str, dict[str, str]] = field(default_factory=dict)
    autofill: dict[str, Any] = field(default_factory=dict)

    @property
    def resume_exists(self) -> bool:
        return bool(self.resume_path) and Path(self.resume_path).is_file()


def ensure_dirs() -> None:
    """Create the app directory and sessions sub-directory if missing."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> AppConfig:
    """Load config from disk, creating defaults if file is absent."""
    ensure_dirs()
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
    raw = json.loads(CONFIG_PATH.read_text())
    return AppConfig(
        resume_path=raw.get("resume_path", ""),
        name=raw.get("name", ""),
        email=raw.get("email", ""),
        phone=raw.get("phone", ""),
        location=raw.get("location", "India"),
        experience_years=raw.get("experience_years", 3),
        credentials=raw.get("credentials", {}),
        autofill=raw.get("autofill", {}),
    )


def save_config(config: AppConfig) -> None:
    """Persist the current config to disk."""
    ensure_dirs()
    data = {
        "resume_path": config.resume_path,
        "name": config.name,
        "email": config.email,
        "phone": config.phone,
        "location": config.location,
        "experience_years": config.experience_years,
        "credentials": config.credentials,
    }
    CONFIG_PATH.write_text(json.dumps(data, indent=2))


def get_user_agent() -> str:
    """Return a Firefox user-agent string matching the current OS."""
    os_name = platform.system()
    if os_name == "Windows":
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
            "Gecko/20100101 Firefox/128.0"
        )
    elif os_name == "Darwin":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) "
            "Gecko/20100101 Firefox/128.0"
        )
    else:
        return (
            "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
            "Gecko/20100101 Firefox/128.0"
        )
