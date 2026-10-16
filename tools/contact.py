"""
Contact extraction utility for job listings and career descriptions.

Enables agents to find recruiter emails, application URLs, and contact
information directly from job descriptions for automated outreach via Gmail/Mailbox MCP.
"""

from __future__ import annotations

import re
from typing import Any

EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", re.IGNORECASE
)
URL_REGEX = re.compile(
    r"https?://(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*)",
    re.IGNORECASE,
)
PHONE_REGEX = re.compile(
    r"(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?)?\d{3,5}[-.\s]?\d{3,5}\b"
)

# Common recruiter phrases
RECRUITER_PHRASES = [
    r"reach out to (?:the recruiter )?([A-Z][a-z]+ [A-Z][a-z]+)",
    r"contact (?:our recruiter )?([A-Z][a-z]+ [A-Z][a-z]+)",
    r"recruiter:\s*([A-Z][a-z]+ [A-Z][a-z]+)",
    r"hiring manager:\s*([A-Z][a-z]+ [A-Z][a-z]+)",
]

# Excluded generic email prefixes that are typically not recruiter mailboxes
GENERIC_IGNORE_EMAILS = {
    "support@github.com",
    "noreply@linkedin.com",
    "no-reply@glassdoor.com",
    "support@naukri.com",
}


def extract_contacts(text: str) -> dict[str, Any]:
    """
    Extract recruiter emails, career application links, phone numbers,
    and potential contact names from raw job posting text.
    """
    if not text:
        return {
            "emails": [],
            "recruiter_names": [],
            "external_links": [],
            "phone_numbers": [],
            "has_contacts": False,
        }

    raw_emails = set(EMAIL_REGEX.findall(text))
    # Strip trailing punctuation from extracted emails
    valid_emails = [
        e.rstrip(".,;:!?)'\"") for e in raw_emails
    ]
    valid_emails = [
        e for e in set(valid_emails)
        if "@" in e and not e.endswith((".png", ".jpg", ".jpeg", ".svg", ".gif"))
        and e.lower() not in GENERIC_IGNORE_EMAILS
    ]

    recruiter_names = []
    for pattern in RECRUITER_PHRASES:
        matches = re.findall(pattern, text)
        for m in matches:
            if m and m not in recruiter_names:
                recruiter_names.append(m)

    raw_urls = set(URL_REGEX.findall(text))
    external_links = [
        u for u in raw_urls
        if any(h in u.lower() for h in ("greenhouse", "lever.co", "workday", "smartrecruiters", "apply", "careers"))
    ]

    phones = set(PHONE_REGEX.findall(text))
    valid_phones = [p.strip() for p in phones if len(re.sub(r"\D", "", p)) >= 10][:3]

    has_contacts = bool(valid_emails or external_links or recruiter_names)

    return {
        "emails": valid_emails,
        "recruiter_names": recruiter_names,
        "external_links": external_links,
        "phone_numbers": valid_phones,
        "has_contacts": has_contacts,
    }
