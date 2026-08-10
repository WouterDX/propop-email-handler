"""
Gmail layer, built on the same pattern as test_gmailaccess.py
(same login / token caching flow), but extended with:
- fetching and parsing full messages (not only metadata)
- fetching full conversation threads as context
- creating/applying labels (to track what is already processed)
- creating draft replies, and optionally sending
"""
from __future__ import annotations

import base64
import logging
import os
from email.mime.text import MIMEText
from typing import Optional

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import config
from email_parser import ParsedEmail, parse_raw_email

log = logging.getLogger(__name__)


def get_gmail_service():
    """Same login logic as in test_gmailaccess.py, with broader scopes
    (see config.GMAIL_SCOPES) so we can also manage labels and drafts."""
    creds = None

    if os.path.exists(config.GMAIL_TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(
                config.GMAIL_TOKEN_FILE, config.GMAIL_SCOPES
            )
        except Exception as e:
            log.warning(
                "Could not read token file at %s (%s). "
                "Try deleting this token file and run again to re-authenticate.",
                config.GMAIL_TOKEN_FILE,
                e,
            )
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                log.warning(
                    "Refreshing Gmail token failed (%s). "
                    "Try deleting %s and run again to re-authenticate.",
                    e,
                    config.GMAIL_TOKEN_FILE,
                )
                creds = None
        else:
            creds = None

        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GMAIL_CREDENTIALS_FILE, config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(config.GMAIL_TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def list_new_message_ids(service, query: str = None, max_results: int = None) -> list[str]:
    query = query or config.GMAIL_QUERY    
    max_results = max_results or config.GMAIL_MAX_RESULTS
    results = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )    
    return [m["id"] for m in results.get("messages", [])]


def get_parsed_message(service, msg_id: str) -> ParsedEmail:
    raw = (
        service.users()
        .messages()
        .get(userId="me", id=msg_id, format="raw")
        .execute()
    )
    raw_bytes = base64.urlsafe_b64decode(raw["raw"])
    parsed = parse_raw_email(raw_bytes)
    parsed.gmail_msg_id = msg_id
    parsed.thread_id = raw.get("threadId")
    return parsed


def get_thread_messages(service, thread_id: str, limit: int = None) -> list[ParsedEmail]:
    """Fetch a full conversation, oldest first, limited to the last
    `limit` messages (see config.THREAD_CONTEXT_LIMIT) as requested in the
    instructions (use context, but avoid unbounded growth)."""
    limit = limit or config.THREAD_CONTEXT_LIMIT
    thread = service.users().threads().get(userId="me", id=thread_id, format="minimal").execute()    
    parsed_messages = []
    for m in thread.get("messages", []):
        raw_msg = (
            service.users()
            .messages()
            .get(userId="me", id=m["id"], format="raw")
            .execute()
        )
        raw_bytes = base64.urlsafe_b64decode(raw_msg["raw"])
        parsed = parse_raw_email(raw_bytes)
        parsed.gmail_msg_id = m["id"]
        parsed.thread_id = thread_id
        parsed_messages.append(parsed)
    # Gmail already returns threads chronologically; we only trim the tail.
    return parsed_messages[-limit:]


# --- Labels -----------------------------------------------------------

_label_cache: dict[str, str] = {}


def ensure_label(service, label_name: str) -> str:
    """Return the label ID; create the label (including optional 'Propop/Xxx'
    parent) if it does not exist yet."""
    if label_name in _label_cache:
        return _label_cache[label_name]

    existing = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in existing:
        if lbl["name"] == label_name:
            _label_cache[label_name] = lbl["id"]
            return lbl["id"]

    created = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        .execute()
    )
    _label_cache[label_name] = created["id"]
    return created["id"]


def add_label(service, msg_id: str, label_name: str):
    label_id = ensure_label(service, label_name)
    service.users().messages().modify(
        userId="me", id=msg_id, body={"addLabelIds": [label_id]}
    ).execute()


# --- Replies ---------------------------------------------------------


def _build_reply_mime(parsed_original: ParsedEmail, body_text: str) -> MIMEText:
    subject = parsed_original.subject or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    mime = MIMEText(body_text, "plain", "utf-8")
    mime["To"] = parsed_original.from_email
    mime["Subject"] = subject
    if parsed_original.message_id:
        mime["In-Reply-To"] = parsed_original.message_id
        refs = parsed_original.references + [parsed_original.message_id]
        mime["References"] = " ".join(refs)
    return mime


def create_draft_reply(service, parsed_original: ParsedEmail, body_text: str) -> dict:
    """Create a DRAFT reply (not sent). This is the app's default behavior:
    a staff member reviews and sends manually."""
    mime = _build_reply_mime(parsed_original, body_text)
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    body = {"message": {"raw": raw, "threadId": parsed_original.thread_id}}
    return service.users().drafts().create(userId="me", body=body).execute()


def send_reply(service, parsed_original: ParsedEmail, body_text: str) -> dict:
    """Send a reply immediately. Only used when config.AUTO_SEND=true.
    Use with appropriate caution -- see SETUP.md."""
    mime = _build_reply_mime(parsed_original, body_text)
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    body = {"raw": raw, "threadId": parsed_original.thread_id}
    return service.users().messages().send(userId="me", body=body).execute()
