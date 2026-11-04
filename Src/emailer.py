"""
Argus - Emailer (draft -> user approval -> Gmail send)

Flow (same shape as downloader.py):
  1. The Research agent calls propose_email(to, subject, body, reason). This
     only VALIDATES the request (address format, length) and queues it for
     the user's approval. Nothing is sent, and Gmail is not even contacted.
  2. The app shows the request as an editable card: to, cc, subject, body.
  3. Only when the USER clicks "Send" does perform_send() sign in to Gmail
     (a one-time browser step, then silent) and actually send it.

Security:
  - Permissions: two Gmail scopes, 'gmail.send' (send mail) and 'gmail.readonly'
    (check replies and search the inbox, e.g. check_inbox). Never contacts. Reading
    is only ever on the user's request; sending always needs the user's click.
  - Human-in-the-loop: the AI can only draft and ask; a human always clicks
    Send, and can edit the draft first.
  - Every sent email is logged (to, subject, time) in data\\email_log.jsonl.
  - A simple per-day send cap, so a bug or bad instruction can't mass-mail.
"""

import json
import os
import re
import uuid
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "data" / "gmail_token.json"
PENDING_FILE = PROJECT_ROOT / "data" / "pending_emails.json"
EMAIL_LOG = PROJECT_ROOT / "data" / "email_log.jsonl"

# Attachments: ONLY files Argus itself made in vault\tailored_cvs. The AI never sees
# file names or contents (it only says "attach the latest CV"); the person picks and
# approves the attachment on the card, and this module reads the file itself.
VAULT_DIR = Path(os.environ.get("VAULT_DIR", PROJECT_ROOT / "vault")).resolve()
ATTACH_DIR = VAULT_DIR / "tailored_cvs"
ATTACH_TYPES = {".pdf": ("application", "pdf"),
                ".docx": ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document")}
MAX_ATTACH_BYTES = 10 * 1024 * 1024
SETTINGS_FILE = PROJECT_ROOT / "data" / "settings.json"     # the app's sidebar stores "sender_name" here

# The AI cannot see the vault, so it does not know the user's name and used to leave
# "[Your Name]" in the email. A name placeholder is filled from the saved name (or removed);
# any other placeholder stays visible and blocks Send in the app until the person edits it.
_NAME_PLACEHOLDER = re.compile(
    r"[\[<{]{1,2}\s*(?:your|my|sender'?s?)\s+(?:full\s+)?name\s*[\]>}]{1,2}", re.I)
_ANY_PLACEHOLDER = re.compile(r"\[[^\]\n]{2,40}\]|\{\{[^}\n]{2,40}\}\}|<[^>\n@]{2,40}>")


def sender_name() -> str:
    try:
        return str(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).get("sender_name", "")).strip()
    except (OSError, ValueError):
        return ""


def find_placeholders(text: str) -> list[str]:
    return [m for m in _ANY_PLACEHOLDER.findall(text or "") if "http" not in m]


def _fill_name(text: str) -> str:
    text = _NAME_PLACEHOLDER.sub(sender_name(), text)
    return re.sub(r"[ \t]+(\n|$)", r"\1", text).strip()

# gmail.send: send mail. gmail.readonly: check for replies -- but the code
# below only ever looks up threads Argus itself sent (from email_log.jsonl),
# never a general inbox search, so read access stays self-limited even
# though the granted scope is broader.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]
MAX_PER_DAY = 20
MAX_PENDING_AGE_HOURS = 24     # forgotten drafts are auto-cancelled after this
MAX_BODY_CHARS = 20000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_addr(addr: str) -> str:
    addr = addr.strip()
    if not EMAIL_RE.match(addr):
        raise ValueError(f"'{addr}' doesn't look like a valid email address.")
    return addr


# ------------------------------------------------------------------ pending queue
def _read_pending() -> list[dict]:
    """Load pending drafts, quietly dropping ones older than the age limit."""
    try:
        items = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    fresh = [it for it in items if age_hours(it) < MAX_PENDING_AGE_HOURS]
    if len(fresh) != len(items):
        _write_pending(fresh)
    return fresh


def age_hours(item: dict) -> float:
    try:
        requested = datetime.fromisoformat(item["requested"])
    except (KeyError, ValueError):
        return 0.0
    return (datetime.now() - requested).total_seconds() / 3600


def age_str(item: dict) -> str:
    hours = age_hours(item)
    if hours < 1 / 60:
        return "just now"
    if hours < 1:
        return f"{round(hours * 60)} min ago"
    if hours < 24:
        return f"{round(hours)} hr ago"
    return f"{round(hours / 24)} day(s) ago"


def _write_pending(items: list[dict]) -> None:
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


def list_attachments() -> list[str]:
    """Tailored CV files that can be attached, newest first."""
    if not ATTACH_DIR.is_dir():
        return []
    files = [f for f in ATTACH_DIR.iterdir() if f.is_file() and f.suffix.lower() in ATTACH_TYPES]
    return [f.name for f in sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)]


def _attachment_path(name: str) -> Path:
    """Resolve an attachment name; it must be a real, small, allowed file inside tailored_cvs."""
    path = (ATTACH_DIR / name).resolve()
    if path.parent != ATTACH_DIR.resolve() or not path.is_file():
        raise ValueError(f"'{name}' is not a file in the vault's tailored_cvs folder.")
    if path.suffix.lower() not in ATTACH_TYPES:
        raise ValueError("Only PDF or Word files can be attached.")
    if path.stat().st_size > MAX_ATTACH_BYTES:
        raise ValueError("That file is bigger than 10 MB, too big to attach.")
    return path


_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{8,40}$")


def propose(to: str, subject: str, body: str, reason: str = "", cc: str = "",
            attach_latest_cv: bool = False, reply_to_message_id: str = "") -> dict:
    """Validate an email draft and queue it for the user's approval. Sends nothing."""
    reply_to = (reply_to_message_id or "").strip()
    if reply_to and not _MESSAGE_ID.match(reply_to):
        raise ValueError("reply_to_message_id must be the message_id from check_inbox / "
                         "check_email_replies (do not invent one).")
    attachment = ""
    if attach_latest_cv:
        names = list_attachments()
        if not names:
            raise ValueError("There is no tailored CV in the vault (folder tailored_cvs) to "
                             "attach yet. Make one in Vault mode first.")
        attachment = names[0]
    item = {
        "id": uuid.uuid4().hex[:10],
        "to": _validate_addr(to),
        "cc": _validate_addr(cc) if cc.strip() else "",
        "subject": _fill_name(subject),
        "body": _fill_name(body),
        "reason": reason[:200],
        "attachment": attachment,
        "reply_to": reply_to,
        "requested": datetime.now().isoformat(timespec="seconds"),
    }
    if not item["subject"]:
        raise ValueError("Subject can't be empty.")
    if not item["body"]:
        raise ValueError("Body can't be empty.")
    if len(item["body"]) > MAX_BODY_CHARS:
        raise ValueError("Email body is too long.")
    _write_pending(_read_pending() + [item])
    return item


def list_pending() -> list[dict]:
    return _read_pending()


def cancel(request_id: str) -> None:
    _write_pending([p for p in _read_pending() if p["id"] != request_id])


def update(request_id: str, **fields) -> dict:
    """Apply user edits (to/cc/subject/body) to a queued draft before sending."""
    items = _read_pending()
    for item in items:
        if item["id"] != request_id:
            continue
        for key, value in fields.items():
            if key == "to":
                value = _validate_addr(value)
            elif key == "cc" and value.strip():
                value = _validate_addr(value)
            elif key == "attachment":
                value = _attachment_path(value).name if value else ""
            item[key] = value
        _write_pending(items)
        return item
    raise ValueError("This email request no longer exists.")


# ------------------------------------------------------------------ sending
def _today_count() -> int:
    if not EMAIL_LOG.exists():
        return 0
    today = date.today().isoformat()
    lines = EMAIL_LOG.read_text(encoding="utf-8").splitlines()
    return sum(1 for line in lines if json.loads(line).get("time", "").startswith(today))


def _get_service():
    """
    Return an authenticated Gmail client. The FIRST call opens a browser for a
    one-time Google sign-in; the token is then cached in data\\gmail_token.json
    and silently refreshed after that.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    if not CREDENTIALS_FILE.exists():
        raise RuntimeError(
            f"credentials.json not found at {CREDENTIALS_FILE}. "
            "Follow the Gmail setup steps and place it there.")

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def perform_send(request_id: str) -> dict:
    """Called by the APP when the user clicks Send. Actually sends the email."""
    import base64
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if _today_count() >= MAX_PER_DAY:
        raise RuntimeError(f"Daily limit of {MAX_PER_DAY} emails reached. Try again tomorrow.")

    item = next((p for p in _read_pending() if p["id"] == request_id), None)
    if item is None:
        raise ValueError("This email request no longer exists.")

    reply_to = item.get("reply_to") or ""
    service = _get_service()
    thread_id, in_reply_to, references, subject = None, "", "", item["subject"]
    if reply_to:
        # Only the headers of the mail being answered are read (no body), to place the
        # answer in the SAME Gmail conversation, like pressing "Reply".
        original = service.users().messages().get(
            userId="me", id=reply_to, format="metadata",
            metadataHeaders=["Message-ID", "References", "Subject"]).execute()
        hdr = {h["name"].lower(): h["value"] for h in original.get("payload", {}).get("headers", [])}
        thread_id = original.get("threadId")
        in_reply_to = hdr.get("message-id", "")
        references = " ".join(x for x in (hdr.get("references", ""), in_reply_to) if x)
        if not subject.lower().startswith("re:"):
            subject = "Re: " + (hdr.get("subject") or subject)
    attachment = item.get("attachment") or ""
    if attachment:
        path = _attachment_path(attachment)               # re-checked at send time
        msg = MIMEMultipart()
        msg.attach(MIMEText(item["body"]))
        main, sub = ATTACH_TYPES[path.suffix.lower()]
        part = MIMEApplication(path.read_bytes(), _subtype=sub)
        part.add_header("Content-Disposition", "attachment", filename=path.name)
        msg.attach(part)
    else:
        msg = MIMEText(item["body"])
    msg["to"] = item["to"]
    msg["subject"] = subject
    if item.get("cc"):
        msg["cc"] = item["cc"]
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body = {"raw": raw, **({"threadId": thread_id} if thread_id else {})}
    sent = service.users().messages().send(userId="me", body=body).execute()

    EMAIL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with EMAIL_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "time": datetime.now().isoformat(timespec="seconds"),
            "to": item["to"], "subject": subject, "attachment": attachment, "reply_to": reply_to,
            "gmail_id": sent.get("id"), "thread_id": sent.get("threadId"),
        }, ensure_ascii=False) + "\n")

    cancel(request_id)
    return {"sent_to": item["to"], "subject": subject, "attachment": attachment,
            "as_reply": bool(reply_to)}


# ------------------------------------------------------------------ checking replies
def search_inbox(query: str, max_results: int = 10) -> list[dict]:
    """
    Search the WHOLE inbox using Gmail's own search syntax, e.g.
    "from:salman", "salman job", "subject:interview", "is:unread after:2026/09/01".
    Unlike check_replies, this is a general search -- it can find anything in
    the mailbox, not just threads Argus itself sent.
    """
    service = _get_service()
    max_results = max(1, min(max_results, 25))
    found = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute().get("messages", [])

    results = []
    for m in found:
        msg = service.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        results.append({
            "message_id": m["id"],
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "snippet": msg.get("snippet", ""),
        })
    return results


def _sent_log() -> list[dict]:
    if not EMAIL_LOG.exists():
        return []
    return [json.loads(line) for line in EMAIL_LOG.read_text(encoding="utf-8").splitlines() if line.strip()]


def check_replies(to: str = "", subject_contains: str = "", limit: int = 10) -> list[dict]:
    """
    Check for replies on emails ARGUS ITSELF SENT (never a general inbox
    search). Matches are looked up by the Gmail thread id recorded when each
    email was sent, so this can only ever see threads Argus started.
    """
    sent = [s for s in _sent_log() if s.get("thread_id")]
    if to:
        sent = [s for s in sent if s["to"].lower() == to.strip().lower()]
    if subject_contains:
        sent = [s for s in sent if subject_contains.lower() in s["subject"].lower()]
    sent = sent[-limit:]                          # most recent first, capped
    if not sent:
        return []

    service = _get_service()
    results = []
    for entry in reversed(sent):
        result = {"to": entry["to"], "subject": entry["subject"],
                  "sent_time": entry["time"], "replied": False}
        try:
            thread = service.users().threads().get(
                userId="me", id=entry["thread_id"], format="metadata",
                metadataHeaders=["From", "Date"],
            ).execute()
            messages = thread.get("messages", [])
            if len(messages) > 1:
                last = messages[-1]
                headers = {h["name"]: h["value"] for h in last["payload"]["headers"]}
                result.update(replied=True, reply_from=headers.get("From"),
                              reply_date=headers.get("Date"), snippet=last.get("snippet", ""),
                              message_id=last.get("id"))
        except Exception as e:
            result["error"] = str(e)
        results.append(result)
    return results
