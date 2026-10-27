"""Read emails from Gmail (LinkedIn job alerts, replies from companies) via the Gmail API or IMAP.

Read-only: the Gmail API path uses the gmail.readonly scope, and IMAP opens the mailbox
with readonly=True, so nothing is marked read, moved, or deleted.
"""

from __future__ import annotations

import base64
import email
import imaplib
from dataclasses import dataclass
from email.policy import default as default_policy

import httpx

from ..settings import env

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


@dataclass
class Message:
    id: str
    subject: str
    html: str
    text: str
    sender: str = ""        # "Name <address>"
    date: str = ""          # RFC 2822 date header
    message_id: str = ""    # Message-ID header, for Gmail links and de-duplication

    @property
    def gmail_link(self) -> str:
        mid = self.message_id.strip("<>")
        return f"https://mail.google.com/mail/u/0/#search/rfc822msgid%3A{mid}" if mid else ""


class MailboxError(RuntimeError):
    pass


def configured() -> str | None:
    if env.gmail_oauth:
        return "gmail_api"
    if env.imap:
        return "imap"
    return None


def fetch(query: str, max_messages: int) -> list[Message]:
    method = configured()
    if method == "gmail_api":
        return _gmail_api(query, max_messages)
    if method == "imap":
        return _imap(query, max_messages)
    return []


# ------------------------------------------------------------------ Gmail API

def gmail_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    resp = httpx.post(TOKEN_URL, data={
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token",
    }, timeout=30)
    if resp.status_code != 200:
        raise MailboxError(f"Gmail token refresh failed ({resp.status_code}): {resp.text[:200]}")
    return resp.json()["access_token"]


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _walk(part: dict, out: dict[str, str]) -> None:
    mime = part.get("mimeType", "")
    data = (part.get("body") or {}).get("data")
    if data and mime in ("text/html", "text/plain") and mime not in out:
        out[mime] = _decode(data)
    for child in part.get("parts") or []:
        _walk(child, out)


def _gmail_api(query: str, max_messages: int) -> list[Message]:
    token = gmail_access_token(*env.gmail_oauth)  # type: ignore[misc]
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=30, headers=headers) as http:
        resp = http.get(f"{GMAIL_API}/messages", params={"q": query, "maxResults": max_messages})
        if resp.status_code != 200:
            raise MailboxError(f"Gmail search failed ({resp.status_code}): {resp.text[:200]}")
        messages = []
        for ref in resp.json().get("messages", []):
            full = http.get(f"{GMAIL_API}/messages/{ref['id']}", params={"format": "full"}).json()
            payload = full.get("payload") or {}
            hdr = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
            bodies: dict[str, str] = {}
            _walk(payload, bodies)
            messages.append(Message(ref["id"], hdr.get("subject", ""), bodies.get("text/html", ""),
                                    bodies.get("text/plain", ""), hdr.get("from", ""), hdr.get("date", ""),
                                    hdr.get("message-id", "")))
    return messages


# ------------------------------------------------------------------ IMAP

def _imap(query: str, max_messages: int) -> list[Message]:
    host, user, password = env.imap  # type: ignore[misc]
    try:
        conn = imaplib.IMAP4_SSL(host)
        conn.login(user, password)
    except (imaplib.IMAP4.error, OSError) as e:
        raise MailboxError(f"IMAP login to {host} failed: {e}") from e
    try:
        gmail = "gmail" in host or "google" in host
        # All Mail, so alerts that were archived or labelled out of the inbox are still found.
        status, _ = conn.select('"[Gmail]/All Mail"' if gmail else "INBOX", readonly=True)
        if status != "OK":
            conn.select("INBOX", readonly=True)
        if gmail:
            # Sent as a UTF-8 literal, not a quoted string: the query itself has quotes in it ("Acme Corp")
            # and company names can have accents, and either one breaks a quoted string.
            conn.literal = query.encode("utf-8")
            status, data = conn.search("UTF-8", "X-GM-RAW")
        else:
            status, data = conn.search(None, '(FROM "linkedin.com")')
        if status != "OK":
            raise MailboxError(f"IMAP search failed: {status}")
        ids = data[0].split()[-max_messages:]
        messages = []
        for mid in reversed(ids):
            status, parts = conn.fetch(mid, "(BODY.PEEK[])")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            msg = email.message_from_bytes(parts[0][1], policy=default_policy)
            html_part = msg.get_body(("html",))
            text_part = msg.get_body(("plain",))
            messages.append(Message(
                id=str(msg.get("Message-ID") or mid.decode()),
                subject=str(msg.get("Subject") or ""),
                html=html_part.get_content() if html_part else "",
                text=text_part.get_content() if text_part else "",
                sender=str(msg.get("From") or ""),
                date=str(msg.get("Date") or ""),
                message_id=str(msg.get("Message-ID") or ""),
            ))
        return messages
    finally:
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
