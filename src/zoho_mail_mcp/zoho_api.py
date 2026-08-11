"""Zoho Mail REST API client.

Uses https://mail.zoho.com/api with OAuth 2.0 refresh tokens. Unlike
IMAP/SMTP (paid plans only), the REST API also serves free-plan mailboxes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from .config import OAUTH_SCOPES, Account


class MailError(RuntimeError):
    """A mail operation failed; the message is safe to surface to the agent."""


@dataclass
class EmailSummary:
    message_id: str
    folder_id: str
    sender: str
    from_address: str
    subject: str
    received: str
    unread: bool
    size: int
    has_attachment: bool
    snippet: str


@dataclass
class AttachmentInfo:
    index: int
    attachment_id: str
    name: str
    size: int


@dataclass
class FullEmail:
    message_id: str
    folder_id: str
    sender: str
    from_address: str
    to_address: str
    cc_address: str
    subject: str
    received: str
    unread: bool
    body: str
    attachments: list[AttachmentInfo] = field(default_factory=list)


def _format_received(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return str(ms) if ms else "(no date)"


def _is_unread(entry: dict) -> bool:
    # In message listings, status "0" = unread, "1" = read.
    return str(entry.get("status", "1")) == "0"


def build_search_key(
    unread_only: bool = False,
    from_contains: str | None = None,
    subject_contains: str | None = None,
    text_contains: str | None = None,
) -> str:
    """Compose Zoho's '::'-joined searchKey syntax from simple filters."""
    parts: list[str] = []
    if from_contains:
        parts.append(f"sender:{from_contains}")
    if subject_contains:
        parts.append(f"subject:{subject_contains}")
    if text_contains:
        parts.append(f"entire:{text_contains}")
    if unread_only:
        parts.append("newMails")
    return "::".join(parts)


# Access tokens live ~1h; cache them per account name across tool calls.
_token_cache: dict[str, tuple[str, float]] = {}


class ZohoClient:
    def __init__(self, acct: Account) -> None:
        self.acct = acct
        self._account_id: str | None = None
        self._folders: list[dict] | None = None

    # --- auth & transport -------------------------------------------------

    async def _access_token(self) -> str:
        cached = _token_cache.get(self.acct.name)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self.acct.accounts_base}/oauth/v2/token",
                    data={  # body, not query params: keeps secrets out of URL logs
                        "grant_type": "refresh_token",
                        "refresh_token": self.acct.refresh_token,
                        "client_id": self.acct.client_id,
                        "client_secret": self.acct.client_secret,
                    },
                )
        except httpx.HTTPError as exc:
            raise MailError(
                f"Could not reach {self.acct.accounts_base} to refresh the OAuth token: {exc}"
            ) from exc
        try:
            data = resp.json()
        except ValueError:
            raise MailError(
                f"OAuth token endpoint returned non-JSON (HTTP {resp.status_code})."
            ) from None
        token = data.get("access_token")
        if not token:
            raise MailError(
                f"OAuth refresh failed for {self.acct.email}: {data.get('error', data)}. "
                "Check client_id / client_secret / refresh_token, that the scopes were "
                f"{OAUTH_SCOPES}, and that accounts_base matches the account's data "
                f"center (currently {self.acct.accounts_base})."
            )
        _token_cache[self.acct.name] = (token, time.time() + int(data.get("expires_in", 3600)))
        return token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        expect_bytes: bool = False,
    ) -> Any:
        token = await self._access_token()
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.request(
                    method,
                    f"{self.acct.api_base}{path}",
                    params=params,
                    json=json,
                    headers={"Authorization": f"Zoho-oauthtoken {token}"},
                )
        except httpx.HTTPError as exc:
            raise MailError(f"Zoho API request failed ({method} {path}): {exc}") from exc

        if expect_bytes:
            if resp.status_code != 200:
                raise MailError(f"Zoho API error on {path}: HTTP {resp.status_code}.")
            return resp.content

        try:
            body = resp.json()
        except ValueError:
            raise MailError(
                f"Zoho API returned non-JSON (HTTP {resp.status_code}) for {path}. "
                "If this account is on a non-US data center, set api_base accordingly."
            ) from None
        status = body.get("status", {})
        if resp.status_code != 200 or (status and status.get("code") != 200):
            desc = status.get("description") or str(body)[:300]
            hint = ""
            if resp.status_code == 401 or "INVALID_OAUTHTOKEN" in str(desc).upper():
                hint = (
                    " Hint: regenerate the refresh token with scopes "
                    f"{OAUTH_SCOPES} and update accounts.toml."
                )
            raise MailError(f"Zoho API error on {path}: HTTP {resp.status_code}, {desc}.{hint}")
        return body.get("data")

    # --- account & folders ---------------------------------------------------

    async def account_id(self) -> str:
        if self._account_id:
            return self._account_id
        data = await self._request("GET", "/api/accounts")
        entries = data if isinstance(data, list) else [data]
        wanted = self.acct.email.lower()
        chosen = None
        for entry in entries:
            addresses = {str(entry.get("primaryEmailAddress", "")).lower(),
                         str(entry.get("mailboxAddress", "")).lower()}
            for item in entry.get("emailAddress", []) or []:
                if isinstance(item, dict):
                    addresses.add(str(item.get("mailId", "")).lower())
            if wanted in addresses:
                chosen = entry
                break
        if chosen is None and len(entries) == 1:
            chosen = entries[0]
        if chosen is None or not chosen.get("accountId"):
            raise MailError(
                f"Could not find a Zoho mailbox matching {self.acct.email} via this "
                "refresh token. The token authorizes whichever user generated it — "
                "make sure it was created while logged in to this mailbox's account."
            )
        self._account_id = str(chosen["accountId"])
        return self._account_id

    async def folders(self) -> list[dict]:
        if self._folders is None:
            account_id = await self.account_id()
            data = await self._request("GET", f"/api/accounts/{account_id}/folders")
            self._folders = data if isinstance(data, list) else []
        return self._folders

    async def resolve_folder(self, name: str) -> tuple[str, str]:
        """Folder name (or path) -> (folderId, canonical name)."""
        wanted = name.strip().strip("/").lower()
        for entry in await self.folders():
            folder_name = str(entry.get("folderName", ""))
            path = str(entry.get("path", "")).strip("/")
            if wanted in (folder_name.lower(), path.lower()):
                return str(entry.get("folderId")), folder_name
        names = ", ".join(str(e.get("folderName")) for e in await self.folders())
        raise MailError(f"Folder {name!r} not found on {self.acct.email}. Folders: {names}")

    # --- reading ----------------------------------------------------------------

    def _summary_from_entry(self, entry: dict) -> EmailSummary:
        snippet = str(entry.get("summary", "")).strip()
        return EmailSummary(
            message_id=str(entry.get("messageId", "")),
            folder_id=str(entry.get("folderId", "")),
            sender=str(entry.get("sender", "")),
            from_address=str(entry.get("fromAddress", "")),
            subject=str(entry.get("subject", "")) or "(no subject)",
            received=_format_received(entry.get("receivedTime")),
            unread=_is_unread(entry),
            size=int(entry.get("size", 0) or 0),
            has_attachment=str(entry.get("hasAttachment", "0")) == "1",
            snippet=snippet[:150],
        )

    async def list_messages(self, folder_id: str, limit: int, unread_only: bool) -> list[EmailSummary]:
        account_id = await self.account_id()
        params: dict[str, Any] = {
            "folderId": folder_id,
            "limit": limit,
            "start": 1,
            "sortBy": "date",
            "sortorder": "false",  # descending: newest first
        }
        if unread_only:
            params["status"] = "unread"
        data = await self._request("GET", f"/api/accounts/{account_id}/messages/view", params=params)
        return [self._summary_from_entry(e) for e in data or []]

    async def search_messages(self, search_key: str, limit: int) -> list[EmailSummary]:
        account_id = await self.account_id()
        params = {"searchKey": search_key, "limit": limit, "start": 1, "includeto": "true"}
        data = await self._request("GET", f"/api/accounts/{account_id}/messages/search", params=params)
        return [self._summary_from_entry(e) for e in data or []]

    async def get_message(self, folder_id: str, message_id: str) -> FullEmail:
        from .htmltext import html_to_text

        account_id = await self.account_id()
        base = f"/api/accounts/{account_id}/folders/{folder_id}/messages/{message_id}"
        details = await self._request("GET", f"{base}/details")
        content = await self._request("GET", f"{base}/content", params={"includeBlockContent": "true"})

        raw_body = str((content or {}).get("content", ""))
        body = html_to_text(raw_body) if raw_body else "(empty body)"

        attachments: list[AttachmentInfo] = []
        if str((details or {}).get("hasAttachment", "0")) == "1":
            info = await self._request("GET", f"{base}/attachmentinfo")
            for i, att in enumerate((info or {}).get("attachments", []) or []):
                attachments.append(
                    AttachmentInfo(
                        index=i,
                        attachment_id=str(att.get("attachmentId", "")),
                        name=str(att.get("attachmentName", f"attachment-{i}")),
                        size=int(att.get("attachmentSize", 0) or 0),
                    )
                )

        details = details or {}
        return FullEmail(
            message_id=message_id,
            folder_id=folder_id,
            sender=str(details.get("sender", "")),
            from_address=str(details.get("fromAddress", "")),
            to_address=str(details.get("toAddress", "")),
            cc_address=str(details.get("ccAddress", "")).replace("Not Provided", ""),
            subject=str(details.get("subject", "")) or "(no subject)",
            received=_format_received(details.get("receivedTime")),
            unread=_is_unread(details),
            body=body,
            attachments=attachments,
        )

    async def download_attachment(self, folder_id: str, message_id: str, attachment_id: str) -> bytes:
        account_id = await self.account_id()
        return await self._request(
            "GET",
            f"/api/accounts/{account_id}/folders/{folder_id}/messages/{message_id}"
            f"/attachments/{attachment_id}",
            expect_bytes=True,
        )

    async def set_read_state(self, message_ids: list[str], read: bool) -> None:
        account_id = await self.account_id()
        await self._request(
            "PUT",
            f"/api/accounts/{account_id}/updatemessage",
            json={
                "mode": "markAsRead" if read else "markAsUnread",
                "messageId": [int(m) for m in message_ids],
            },
        )

    # --- sending -----------------------------------------------------------------

    async def send(self, payload: dict, reply_to_message_id: str | None = None) -> None:
        """Send a composed message; replies thread server-side via the reply API."""
        account_id = await self.account_id()
        if reply_to_message_id:
            await self._request(
                "POST",
                f"/api/accounts/{account_id}/messages/{reply_to_message_id}",
                json={"action": "reply", **payload},
            )
        else:
            await self._request("POST", f"/api/accounts/{account_id}/messages", json=payload)

    async def save_draft(self, payload: dict) -> str:
        """Save a composed message to the mailbox's Drafts folder without sending it.

        Uses the same compose endpoint as send() with mode="draft", so the draft
        becomes a real Zoho message you can open and edit in any Zoho client.
        Returns the new messageId.
        """
        account_id = await self.account_id()
        data = await self._request(
            "POST",
            f"/api/accounts/{account_id}/messages",
            json={**payload, "mode": "draft"},
        )
        return str((data or {}).get("messageId", ""))


def build_send_payload(
    acct: Account,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> dict:
    payload = {
        "fromAddress": acct.email,
        "toAddress": ",".join(to),
        "subject": subject,
        "content": body,
        "mailFormat": "plaintext",
    }
    if cc:
        payload["ccAddress"] = ",".join(cc)
    if bcc:
        payload["bccAddress"] = ",".join(bcc)
    return payload
