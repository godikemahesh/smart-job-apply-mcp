"""
Gmail MCP Server — Autonomous Email Outreach, Follow-ups, and Inbox Management.

Supports 16 tools:
- send_email, reply_email, forward_email, create_draft
- list_accounts, authenticate, reauth, remove_account
- search_emails, read_email, read_thread, inbox_summary
- modify_email, batch_modify_emails, trash_emails, bulk_trash, bulk_modify

Transport: stdio
"""

from __future__ import annotations

import asyncio
import base64
import email
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import logging
import mimetypes
import os
from pathlib import Path
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# Google API client imports
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

# Ensure UTF-8 I/O encoding without requiring PYTHONIOENCODING in env
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# Logging (all logs to stderr so stdout is reserved for JSON-RPC MCP frames)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("gmail-mcp")

# Config directories
GMAIL_CONFIG_DIR = Path.home() / ".gmail-mcp"
MAILBOX_CONFIG_DIR = Path.home() / ".mailbox-mcp"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]


def get_config_dir() -> Path:
    """Return ~/.gmail-mcp if it exists or default, or ~/.mailbox-mcp if already populated."""
    if GMAIL_CONFIG_DIR.exists():
        return GMAIL_CONFIG_DIR
    if (MAILBOX_CONFIG_DIR / "oauth-keys.json").exists() or (MAILBOX_CONFIG_DIR / "accounts.json").exists():
        return MAILBOX_CONFIG_DIR
    GMAIL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return GMAIL_CONFIG_DIR


def ensure_config_dirs(config_dir: Path | None = None) -> Path:
    cd = config_dir or get_config_dir()
    cd.mkdir(parents=True, exist_ok=True)
    (cd / "accounts").mkdir(parents=True, exist_ok=True)
    return cd


def load_accounts_config(config_dir: Path | None = None) -> dict[str, Any]:
    cd = config_dir or get_config_dir()
    acc_file = cd / "accounts.json"
    if not acc_file.exists():
        if cd != MAILBOX_CONFIG_DIR and (MAILBOX_CONFIG_DIR / "accounts.json").exists():
            return json.loads((MAILBOX_CONFIG_DIR / "accounts.json").read_text(encoding="utf-8"))
        return {"accounts": {}}
    try:
        return json.loads(acc_file.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read accounts.json: %s", exc)
        return {"accounts": {}}


def save_accounts_config(data: dict[str, Any], config_dir: Path | None = None) -> None:
    cd = ensure_config_dirs(config_dir)
    acc_file = cd / "accounts.json"
    acc_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_oauth_client_info(config_dir: Path | None = None) -> dict[str, Any]:
    cd = config_dir or get_config_dir()
    keys_path = cd / "oauth-keys.json"
    if not keys_path.exists() and (MAILBOX_CONFIG_DIR / "oauth-keys.json").exists():
        keys_path = MAILBOX_CONFIG_DIR / "oauth-keys.json"

    if not keys_path.exists():
        raise FileNotFoundError(
            f"OAuth client configuration not found. Please save your Google OAuth client JSON as '{cd / 'oauth-keys.json'}'."
        )

    raw = json.loads(keys_path.read_text(encoding="utf-8"))
    creds_info = raw.get("installed") or raw.get("web")
    if not creds_info:
        raise ValueError("Invalid oauth-keys.json: missing 'installed' or 'web' credentials.")
    return creds_info


def get_token_path(alias: str, config_dir: Path | None = None) -> Path:
    cd = config_dir or get_config_dir()
    path = cd / "accounts" / alias / "token.json"
    if not path.exists() and (MAILBOX_CONFIG_DIR / "accounts" / alias / "token.json").exists():
        return MAILBOX_CONFIG_DIR / "accounts" / alias / "token.json"
    return path


def get_credentials_for_account(alias: str, config_dir: Path | None = None) -> Credentials:
    token_file = get_token_path(alias, config_dir)
    if not token_file.exists():
        raise FileNotFoundError(
            f"No token found for account alias '{alias}'. Call authenticate(alias='{alias}', email='...') first."
        )

    token_data = json.loads(token_file.read_text(encoding="utf-8"))
    client_info = load_oauth_client_info(config_dir)

    token_data["client_id"] = client_info.get("client_id")
    token_data["client_secret"] = client_info.get("client_secret")

    creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        updated_data = json.loads(creds.to_json())
        token_file.write_text(json.dumps(updated_data, indent=2), encoding="utf-8")

    return creds


def get_gmail_service(alias: str = "personal", config_dir: Path | None = None) -> Resource:
    creds = get_credentials_for_account(alias, config_dir)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Account Operations
# ---------------------------------------------------------------------------

def list_accounts_op() -> dict[str, Any]:
    cfg = load_accounts_config()
    accounts = []
    for alias, details in cfg.get("accounts", {}).items():
        token_path = get_token_path(alias)
        accounts.append({
            "alias": alias,
            "provider": details.get("provider", "gmail"),
            "email": details.get("email", ""),
            "drafts_only": details.get("drafts_only", False),
            "authenticated": token_path.is_file(),
        })
    return {"accounts": accounts, "count": len(accounts)}


def authenticate_op(
    alias: str,
    email_addr: str,
    provider: str = "gmail",
    drafts_only: bool = False,
) -> dict[str, Any]:
    cd = ensure_config_dirs()
    accounts_cfg = load_accounts_config(cd)

    if alias in accounts_cfg.get("accounts", {}):
        raise ValueError(
            f"Refused: account alias '{alias}' already exists. Use reauth to refresh tokens or edit accounts.json."
        )

    client_info = load_oauth_client_info(cd)
    client_config = {"installed": client_info}

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=4895, prompt="consent", access_type="offline")

    account_dir = cd / "accounts" / alias
    account_dir.mkdir(parents=True, exist_ok=True)
    token_path = account_dir / "token.json"
    token_path.write_text(creds.to_json(), encoding="utf-8")

    accounts_cfg.setdefault("accounts", {})[alias] = {
        "provider": provider,
        "email": email_addr,
        "drafts_only": drafts_only,
    }
    save_accounts_config(accounts_cfg, cd)

    return {
        "success": True,
        "alias": alias,
        "email": email_addr,
        "token_saved": str(token_path),
        "note": f"Account '{alias}' ({email_addr}) authenticated successfully.",
    }


def reauth_op(alias: str) -> dict[str, Any]:
    cd = ensure_config_dirs()
    accounts_cfg = load_accounts_config(cd)
    if alias not in accounts_cfg.get("accounts", {}):
        raise ValueError(f"Account alias '{alias}' does not exist. Call authenticate first.")

    client_info = load_oauth_client_info(cd)
    client_config = {"installed": client_info}

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=4895, prompt="consent", access_type="offline")

    account_dir = cd / "accounts" / alias
    account_dir.mkdir(parents=True, exist_ok=True)
    token_path = account_dir / "token.json"
    token_path.write_text(creds.to_json(), encoding="utf-8")

    return {
        "success": True,
        "alias": alias,
        "token_refreshed": True,
        "note": f"Tokens refreshed for account '{alias}'.",
    }


def remove_account_op(alias: str) -> dict[str, Any]:
    cd = ensure_config_dirs()
    accounts_cfg = load_accounts_config(cd)
    if alias not in accounts_cfg.get("accounts", {}):
        raise ValueError(f"Account alias '{alias}' not found in accounts.json.")

    acc_info = accounts_cfg["accounts"][alias]
    if acc_info.get("safety_lock"):
        raise ValueError(f"Refused: account '{alias}' has safety settings enabled. Remove manually from accounts.json.")

    del accounts_cfg["accounts"][alias]
    save_accounts_config(accounts_cfg, cd)

    token_dir = cd / "accounts" / alias
    if token_dir.exists():
        import shutil
        shutil.rmtree(token_dir, ignore_errors=True)

    return {"success": True, "removed_alias": alias}


# ---------------------------------------------------------------------------
# Message Extraction Helpers
# ---------------------------------------------------------------------------

def _extract_header(headers: list[dict[str, str]], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _extract_body(payload: dict[str, Any]) -> str:
    parts = [payload]
    body_text = ""
    while parts:
        part = parts.pop(0)
        mime_type = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            if "text/plain" in mime_type:
                return decoded
            elif not body_text and "text/html" in mime_type:
                body_text = decoded
        if "parts" in part:
            parts.extend(part["parts"])
    return body_text


def _parse_message_summary(msg: dict[str, Any]) -> dict[str, Any]:
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    return {
        "id": msg.get("id"),
        "threadId": msg.get("threadId"),
        "from": _extract_header(headers, "From"),
        "to": _extract_header(headers, "To"),
        "subject": _extract_header(headers, "Subject"),
        "date": _extract_header(headers, "Date"),
        "snippet": msg.get("snippet", ""),
        "labelIds": msg.get("labelIds", []),
    }


# ---------------------------------------------------------------------------
# Message Operations
# ---------------------------------------------------------------------------

def search_emails_op(
    account: str,
    query: str = "",
    folder: str | None = None,
    max_results: int = 20,
) -> dict[str, Any]:
    service = get_gmail_service(account)
    q = query or ""
    if folder:
        q = f"label:{folder} {q}".strip()

    resp = service.users().messages().list(userId="me", q=q, maxResults=min(max_results, 100)).execute()
    messages = resp.get("messages", [])

    results = []
    for m in messages:
        try:
            full = service.users().messages().get(userId="me", id=m["id"], format="metadata").execute()
            results.append(_parse_message_summary(full))
        except Exception:
            results.append({"id": m["id"], "threadId": m.get("threadId")})

    return {
        "account": account,
        "query": q,
        "count": len(results),
        "messages": results,
    }


def read_email_op(account: str, message_id: str) -> dict[str, Any]:
    service = get_gmail_service(account)
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])

    return {
        "id": msg.get("id"),
        "threadId": msg.get("threadId"),
        "from": _extract_header(headers, "From"),
        "to": _extract_header(headers, "To"),
        "cc": _extract_header(headers, "Cc"),
        "subject": _extract_header(headers, "Subject"),
        "date": _extract_header(headers, "Date"),
        "snippet": msg.get("snippet", ""),
        "labelIds": msg.get("labelIds", []),
        "body": _extract_body(payload),
    }


def read_thread_op(account: str, thread_id: str) -> dict[str, Any]:
    service = get_gmail_service(account)
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    messages = []
    for msg in thread.get("messages", []):
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        messages.append({
            "id": msg.get("id"),
            "from": _extract_header(headers, "From"),
            "to": _extract_header(headers, "To"),
            "date": _extract_header(headers, "Date"),
            "snippet": msg.get("snippet", ""),
            "body": _extract_body(payload),
        })

    return {
        "account": account,
        "thread_id": thread_id,
        "message_count": len(messages),
        "messages": messages,
    }


def inbox_summary_op(account: str, max_results: int = 10) -> dict[str, Any]:
    service = get_gmail_service(account)
    profile = service.users().getProfile(userId="me").execute()

    unread_resp = service.users().messages().list(userId="me", q="is:unread", maxResults=1).execute()
    unread_count = unread_resp.get("resultSizeEstimate", 0)

    recent_resp = service.users().messages().list(userId="me", maxResults=max_results).execute()
    recent = []
    for m in recent_resp.get("messages", []):
        try:
            full = service.users().messages().get(userId="me", id=m["id"], format="metadata").execute()
            summary = _parse_message_summary(full)
            subj = summary.get("subject", "").lower()
            from_addr = summary.get("from", "").lower()
            if any(k in subj or k in from_addr for k in ("interview", "application", "recruiter", "job", "career")):
                summary["category"] = "jobs/recruiting"
            elif any(k in subj for k in ("security", "verify", "alert")):
                summary["category"] = "security/alerts"
            elif any(k in from_addr for k in ("newsletter", "digest", "no-reply", "noreply")):
                summary["category"] = "newsletter/notification"
            else:
                summary["category"] = "inbox"
            recent.append(summary)
        except Exception:
            pass

    return {
        "account": account,
        "email": profile.get("emailAddress"),
        "total_messages": profile.get("messagesTotal"),
        "unread_estimate": unread_count,
        "recent_activity": recent,
    }


def _build_mime(
    sender: str,
    to: list[str] | str,
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    attachments: list[str] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> dict[str, str]:
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = ", ".join(to) if isinstance(to, list) else to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    msg.attach(MIMEText(body, "plain" if "<html" not in body.lower() else "html"))

    if attachments:
        for fpath in attachments:
            p = Path(fpath)
            if not p.is_file():
                continue
            ctype, encoding = mimetypes.guess_type(str(p))
            if ctype is None or encoding is not None:
                ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            with open(p, "rb") as fp:
                part = MIMEApplication(fp.read(), _subtype=subtype)
                part.add_header("Content-Disposition", "attachment", filename=p.name)
                msg.attach(part)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return {"raw": raw}


def create_draft_op(
    account: str,
    to: list[str] | str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    thread_id: str | None = None,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    service = get_gmail_service(account)
    profile = service.users().getProfile(userId="me").execute()
    sender_addr = profile.get("emailAddress", "")
    to_list = [to] if isinstance(to, str) else to

    message_body = _build_mime(
        sender=sender_addr,
        to=to_list,
        subject=subject,
        body=body,
        in_reply_to=in_reply_to,
        attachments=attachments,
    )
    draft_payload: dict[str, Any] = {"message": message_body}
    if thread_id:
        draft_payload["message"]["threadId"] = thread_id

    draft = service.users().drafts().create(userId="me", body=draft_payload).execute()
    return {
        "success": True,
        "draft_id": draft.get("id"),
        "message_id": draft.get("message", {}).get("id"),
        "to": to_list,
        "subject": subject,
        "status": "draft_created",
    }


def send_email_op(
    account: str,
    to: list[str] | str,
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    attachments: list[str] | None = None,
    confirm_new_recipient: bool = False,
) -> dict[str, Any]:
    cfg = load_accounts_config()
    acc_info = cfg.get("accounts", {}).get(account, {})
    if acc_info.get("drafts_only", False):
        return create_draft_op(account, to=to, subject=subject, body=body, attachments=attachments)

    to_list = [to] if isinstance(to, str) else to
    service = get_gmail_service(account)
    profile = service.users().getProfile(userId="me").execute()
    sender_addr = profile.get("emailAddress", "")

    message_body = _build_mime(
        sender=sender_addr,
        to=to_list,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        attachments=attachments,
    )
    sent = service.users().messages().send(userId="me", body=message_body).execute()
    return {
        "success": True,
        "message_id": sent.get("id"),
        "threadId": sent.get("threadId"),
        "to": to_list,
        "subject": subject,
        "status": "sent",
    }


def reply_email_op(
    account: str,
    message_id: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    attachments: list[str] | None = None,
    confirm_new_recipient: bool = False,
) -> dict[str, Any]:
    service = get_gmail_service(account)
    orig = service.users().messages().get(userId="me", id=message_id, format="metadata").execute()
    headers = orig.get("payload", {}).get("headers", [])

    reply_to = _extract_header(headers, "Reply-To") or _extract_header(headers, "From")
    orig_subject = _extract_header(headers, "Subject")
    subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"
    orig_msg_id = _extract_header(headers, "Message-ID")

    cfg = load_accounts_config()
    if cfg.get("accounts", {}).get(account, {}).get("drafts_only", False):
        return create_draft_op(
            account,
            to=[reply_to],
            subject=subject,
            body=body,
            in_reply_to=orig_msg_id,
            thread_id=orig.get("threadId"),
            attachments=attachments,
        )

    profile = service.users().getProfile(userId="me").execute()
    sender_addr = profile.get("emailAddress", "")

    message_body = _build_mime(
        sender=sender_addr,
        to=[reply_to],
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        attachments=attachments,
        in_reply_to=orig_msg_id,
        references=orig_msg_id,
    )
    message_body["threadId"] = orig.get("threadId")

    sent = service.users().messages().send(userId="me", body=message_body).execute()
    return {
        "success": True,
        "message_id": sent.get("id"),
        "threadId": sent.get("threadId"),
        "reply_to": reply_to,
        "subject": subject,
    }


def forward_email_op(
    account: str,
    message_id: str,
    to: list[str] | str,
    body: str = "",
    confirm_external_forward: bool = False,
    confirm_new_recipient: bool = False,
) -> dict[str, Any]:
    service = get_gmail_service(account)
    orig = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = orig.get("payload", {}).get("headers", [])

    orig_subject = _extract_header(headers, "Subject")
    subject = orig_subject if orig_subject.lower().startswith("fwd:") else f"Fwd: {orig_subject}"
    orig_body = _extract_body(orig.get("payload", {}))

    forward_body = f"{body}\n\n---------- Forwarded message ---------\n"
    forward_body += f"From: {_extract_header(headers, 'From')}\n"
    forward_body += f"Date: {_extract_header(headers, 'Date')}\n"
    forward_body += f"Subject: {orig_subject}\n"
    forward_body += f"To: {_extract_header(headers, 'To')}\n\n"
    forward_body += orig_body

    return send_email_op(
        account=account,
        to=to,
        subject=subject,
        body=forward_body,
        confirm_new_recipient=confirm_new_recipient,
    )


def modify_email_op(
    account: str,
    message_id: str,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
) -> dict[str, Any]:
    service = get_gmail_service(account)
    body: dict[str, Any] = {
        "addLabelIds": add_labels or [],
        "removeLabelIds": remove_labels or [],
    }
    updated = service.users().messages().modify(userId="me", id=message_id, body=body).execute()
    return {
        "success": True,
        "id": updated.get("id"),
        "labelIds": updated.get("labelIds", []),
    }


def batch_modify_emails_op(
    account: str,
    message_ids: list[str],
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
) -> dict[str, Any]:
    service = get_gmail_service(account)
    body = {
        "ids": message_ids,
        "addLabelIds": add_labels or [],
        "removeLabelIds": remove_labels or [],
    }
    service.users().messages().batchModify(userId="me", body=body).execute()
    return {
        "success": True,
        "modified_count": len(message_ids),
        "added": add_labels or [],
        "removed": remove_labels or [],
    }


def trash_emails_op(account: str, message_ids: list[str] | str) -> dict[str, Any]:
    service = get_gmail_service(account)
    ids = [message_ids] if isinstance(message_ids, str) else message_ids
    trashed = []
    for mid in ids:
        service.users().messages().trash(userId="me", id=mid).execute()
        trashed.append(mid)
    return {"success": True, "trashed_count": len(trashed), "message_ids": trashed}


def bulk_trash_op(account: str, query: str, dry_run: bool = True) -> dict[str, Any]:
    service = get_gmail_service(account)
    resp = service.users().messages().list(userId="me", q=query, maxResults=100).execute()
    messages = resp.get("messages", [])
    ids = [m["id"] for m in messages]

    if dry_run or not ids:
        return {
            "query": query,
            "matching_count": len(ids),
            "dry_run": dry_run,
            "sample_ids": ids[:5],
            "note": "Preview mode. Set dry_run=false to execute trash.",
        }

    service.users().messages().batchModify(
        userId="me",
        body={"ids": ids, "addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX"]},
    ).execute()
    return {"query": query, "trashed_count": len(ids), "dry_run": False}


def bulk_modify_op(
    account: str,
    query: str,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    service = get_gmail_service(account)
    resp = service.users().messages().list(userId="me", q=query, maxResults=100).execute()
    messages = resp.get("messages", [])
    ids = [m["id"] for m in messages]

    if dry_run or not ids:
        return {
            "query": query,
            "matching_count": len(ids),
            "dry_run": dry_run,
            "sample_ids": ids[:5],
            "note": "Preview mode. Set dry_run=false to commit label changes.",
        }

    body = {
        "ids": ids,
        "addLabelIds": add_labels or [],
        "removeLabelIds": remove_labels or [],
    }
    service.users().messages().batchModify(userId="me", body=body).execute()
    return {
        "query": query,
        "modified_count": len(ids),
        "added": add_labels or [],
        "removed": remove_labels or [],
        "dry_run": False,
    }


# ---------------------------------------------------------------------------
# MCP Server Definition
# ---------------------------------------------------------------------------
gmail_server = Server("gmail-mcp")

GMAIL_TOOLS: list[Tool] = [
    Tool(
        name="send_email",
        description=(
            "Send a new email. Refuses new recipients without confirm_new_recipient, "
            "and creates a draft instead on accounts configured draftsOnly."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "default": "personal", "description": "Account alias."},
                "to": {"type": "array", "items": {"type": "string"}, "description": "Recipient email addresses."},
                "subject": {"type": "string", "description": "Email subject."},
                "body": {"type": "string", "description": "Email body (plain text or HTML)."},
                "cc": {"type": "array", "items": {"type": "string"}, "description": "CC recipients."},
                "bcc": {"type": "array", "items": {"type": "string"}, "description": "BCC recipients."},
                "attachments": {"type": "array", "items": {"type": "string"}, "description": "File paths to attach."},
                "confirm_new_recipient": {"type": "boolean", "default": False, "description": "Confirm sending to new addresses."},
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="reply_email",
        description=(
            "Reply to an email message. Extra cc/bcc addresses the account has never written "
            "to need confirm_new_recipient. On draftsOnly accounts a reply draft is created instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "default": "personal", "description": "Account alias."},
                "message_id": {"type": "string", "description": "Message ID to reply to."},
                "body": {"type": "string", "description": "Reply content."},
                "cc": {"type": "array", "items": {"type": "string"}},
                "bcc": {"type": "array", "items": {"type": "string"}},
                "attachments": {"type": "array", "items": {"type": "string"}},
                "confirm_new_recipient": {"type": "boolean", "default": False},
            },
            "required": ["message_id", "body"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="forward_email",
        description=(
            "Forward an email message to new recipients. Forwarding outside the account's own domain "
            "needs confirm_external_forward; recipients the account has never written to need confirm_new_recipient. "
            "On draftsOnly accounts a forward draft is created instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "default": "personal", "description": "Account alias."},
                "message_id": {"type": "string", "description": "Message ID to forward."},
                "to": {"type": "array", "items": {"type": "string"}, "description": "Recipients to forward to."},
                "body": {"type": "string", "default": "", "description": "Optional note to prepend."},
                "confirm_external_forward": {"type": "boolean", "default": False},
                "confirm_new_recipient": {"type": "boolean", "default": False},
            },
            "required": ["message_id", "to"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="create_draft",
        description="Create a draft email.",
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "default": "personal", "description": "Account alias."},
                "to": {"type": "array", "items": {"type": "string"}, "description": "Recipients."},
                "subject": {"type": "string", "description": "Subject."},
                "body": {"type": "string", "description": "Draft body content."},
                "in_reply_to": {"type": "string", "description": "Original message ID if this is a reply draft."},
                "attachments": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="list_accounts",
        description="List all configured email accounts with their provider type and email address.",
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="authenticate",
        description=(
            "Add a new email account. Refuses an alias that already exists (edit accounts.json or use reauth). "
            "For Gmail: opens a browser for OAuth. For IMAP/JMAP: stores encrypted credentials. "
            "Sensitive fields (username, password) can also be set via environment variables."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "alias": {"type": "string", "description": "Short account alias (e.g. 'personal', 'work')."},
                "provider": {"type": "string", "default": "gmail", "enum": ["gmail"]},
                "email": {"type": "string", "description": "Email address."},
                "drafts_only": {"type": "boolean", "default": False},
            },
            "required": ["alias", "email"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="reauth",
        description=(
            "Re-run OAuth for an existing Gmail account without removing it. "
            "Opens a browser for Google sign-in. Use when the refresh token expires (invalid_grant) or scopes change."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "alias": {"type": "string", "default": "personal", "description": "Account alias to re-authenticate."},
            },
            "required": ["alias"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="remove_account",
        description=(
            "Remove a configured email account and its stored credentials. "
            "Refuses for an account with any safety setting; those are removed by editing accounts.json."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "alias": {"type": "string", "description": "Account alias to remove."},
            },
            "required": ["alias"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="search_emails",
        description=(
            "Search emails in an account. Gmail supports full Gmail search syntax. "
            "Optional folder parameter scopes the search to a specific label/folder."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "default": "personal", "description": "Account alias."},
                "query": {"type": "string", "default": "", "description": "Gmail search query."},
                "folder": {"type": "string", "description": "Optional label name to scope the search."},
                "max_results": {"type": "integer", "default": 20, "description": "Maximum messages to return."},
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="read_email",
        description="Read a single email message with full content.",
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "default": "personal", "description": "Account alias."},
                "message_id": {"type": "string", "description": "Message ID to read."},
            },
            "required": ["message_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="read_thread",
        description="Read an entire email conversation thread (Gmail and JMAP only).",
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "default": "personal", "description": "Account alias."},
                "thread_id": {"type": "string", "description": "Thread ID to read."},
            },
            "required": ["thread_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="inbox_summary",
        description=(
            "Get a summary of recent inbox activity including total and unread counts. "
            "Recent messages are categorized (jobs/recruiting, alerts, newsletters, inbox)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "default": "personal", "description": "Account alias."},
                "max_results": {"type": "integer", "default": 10, "description": "Recent messages count."},
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="modify_email",
        description="Add or remove labels/flags on a message.",
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "default": "personal", "description": "Account alias."},
                "message_id": {"type": "string", "description": "Message ID."},
                "add_labels": {"type": "array", "items": {"type": "string"}, "description": "Labels to add."},
                "remove_labels": {"type": "array", "items": {"type": "string"}, "description": "Labels to remove."},
            },
            "required": ["message_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="batch_modify_emails",
        description="Add or remove labels/flags on multiple messages.",
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "default": "personal", "description": "Account alias."},
                "message_ids": {"type": "array", "items": {"type": "string"}, "description": "Message IDs."},
                "add_labels": {"type": "array", "items": {"type": "string"}},
                "remove_labels": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["message_ids"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="trash_emails",
        description="Move messages to trash.",
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "default": "personal", "description": "Account alias."},
                "message_ids": {"type": "array", "items": {"type": "string"}, "description": "Message IDs to trash."},
            },
            "required": ["message_ids"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="bulk_trash",
        description=(
            "Trash all messages matching a query. Paginates the search and batch-trashes the IDs in one call. "
            "Use dry_run to see the count before committing. Useful for label cleanups (e.g. trash everything in 'Newsletters' or older than 90 days)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "default": "personal", "description": "Account alias."},
                "query": {"type": "string", "description": "Search query for messages to trash."},
                "dry_run": {"type": "boolean", "default": True, "description": "Preview without trashing."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="bulk_modify",
        description=(
            "Add or remove labels on all messages matching a query. Same fast search-then-batch pattern as bulk_trash, "
            "but for arbitrary label ops. Use this for archive (remove_labels=['INBOX']), bulk star/unstar, "
            "mark-read across a label, moving messages between labels, etc. Use dry_run to see the count first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "default": "personal", "description": "Account alias."},
                "query": {"type": "string", "description": "Search query."},
                "add_labels": {"type": "array", "items": {"type": "string"}},
                "remove_labels": {"type": "array", "items": {"type": "string"}},
                "dry_run": {"type": "boolean", "default": True, "description": "Preview count before applying."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
]


@gmail_server.list_tools()
async def list_tools() -> list[Tool]:
    return GMAIL_TOOLS


@gmail_server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    logger.info("Gmail Tool called: %s", name)
    account = arguments.get("account", "personal")

    try:
        if name == "send_email":
            res = send_email_op(
                account=account,
                to=arguments["to"],
                subject=arguments["subject"],
                body=arguments["body"],
                cc=arguments.get("cc"),
                bcc=arguments.get("bcc"),
                attachments=arguments.get("attachments"),
                confirm_new_recipient=arguments.get("confirm_new_recipient", False),
            )
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        elif name == "reply_email":
            res = reply_email_op(
                account=account,
                message_id=arguments["message_id"],
                body=arguments["body"],
                cc=arguments.get("cc"),
                bcc=arguments.get("bcc"),
                attachments=arguments.get("attachments"),
                confirm_new_recipient=arguments.get("confirm_new_recipient", False),
            )
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        elif name == "forward_email":
            res = forward_email_op(
                account=account,
                message_id=arguments["message_id"],
                to=arguments["to"],
                body=arguments.get("body", ""),
                confirm_external_forward=arguments.get("confirm_external_forward", False),
                confirm_new_recipient=arguments.get("confirm_new_recipient", False),
            )
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        elif name == "create_draft":
            res = create_draft_op(
                account=account,
                to=arguments["to"],
                subject=arguments["subject"],
                body=arguments["body"],
                in_reply_to=arguments.get("in_reply_to"),
                attachments=arguments.get("attachments"),
            )
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        elif name == "list_accounts":
            res = list_accounts_op()
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        elif name == "authenticate":
            res = authenticate_op(
                alias=arguments["alias"],
                email_addr=arguments["email"],
                provider=arguments.get("provider", "gmail"),
                drafts_only=arguments.get("drafts_only", False),
            )
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        elif name == "reauth":
            res = reauth_op(alias=arguments.get("alias", "personal"))
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        elif name == "remove_account":
            res = remove_account_op(alias=arguments["alias"])
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        elif name == "search_emails":
            res = search_emails_op(
                account=account,
                query=arguments.get("query", ""),
                folder=arguments.get("folder"),
                max_results=arguments.get("max_results", 20),
            )
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        elif name == "read_email":
            res = read_email_op(account=account, message_id=arguments["message_id"])
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        elif name == "read_thread":
            res = read_thread_op(account=account, thread_id=arguments["thread_id"])
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        elif name == "inbox_summary":
            res = inbox_summary_op(account=account, max_results=arguments.get("max_results", 10))
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        elif name == "modify_email":
            res = modify_email_op(
                account=account,
                message_id=arguments["message_id"],
                add_labels=arguments.get("add_labels"),
                remove_labels=arguments.get("remove_labels"),
            )
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        elif name == "batch_modify_emails":
            res = batch_modify_emails_op(
                account=account,
                message_ids=arguments["message_ids"],
                add_labels=arguments.get("add_labels"),
                remove_labels=arguments.get("remove_labels"),
            )
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        elif name == "trash_emails":
            res = trash_emails_op(account=account, message_ids=arguments["message_ids"])
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        elif name == "bulk_trash":
            res = bulk_trash_op(account=account, query=arguments["query"], dry_run=arguments.get("dry_run", True))
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        elif name == "bulk_modify":
            res = bulk_modify_op(
                account=account,
                query=arguments["query"],
                add_labels=arguments.get("add_labels"),
                remove_labels=arguments.get("remove_labels"),
                dry_run=arguments.get("dry_run", True),
            )
            return [TextContent(type="text", text=json.dumps(res, indent=2))]

        else:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    except Exception as exc:
        logger.exception("Gmail Tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]


# ---------------------------------------------------------------------------
# Server Entry Point
# ---------------------------------------------------------------------------

async def main() -> None:
    logger.info("Starting gmail-mcp server (stdio transport)")
    async with stdio_server() as (read_stream, write_stream):
        await gmail_server.run(
            read_stream,
            write_stream,
            gmail_server.create_initialization_options(),
        )


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()

