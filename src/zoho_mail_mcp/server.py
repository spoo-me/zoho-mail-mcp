"""FastMCP server exposing Zoho Mail tools (REST API transport)."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import drafts
from .config import ConfigError, init_config, load_accounts, resolve_account
from .zoho_api import MailError, ZohoClient, build_search_key, build_send_payload

mcp = FastMCP("zoho_mail_mcp")

BODY_PREVIEW_LIMIT = 20_000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --- helpers -----------------------------------------------------------------


def _error(exc: Exception) -> str:
    if isinstance(exc, (ConfigError, MailError, drafts.DraftError)):
        return f"Error: {exc}"
    return f"Error: unexpected {type(exc).__name__}: {exc}"


def _format_size(size: int) -> str:
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1_000:
        return f"{size / 1_000:.0f} KB"
    return f"{size} B"


def _validate_addresses(kind: str, values: list[str]) -> list[str]:
    for addr in values:
        if not _EMAIL_RE.match(addr):
            raise ValueError(f"{kind} entry {addr!r} is not a plain email address")
    return values


def _format_summaries(header: str, summaries: list) -> str:
    if not summaries:
        return f"{header}\n\nNo messages match."
    lines = [header, "", f"Showing {len(summaries)} message(s), newest first.", ""]
    for s in summaries:
        status = " · UNREAD" if s.unread else ""
        clip = " · 📎" if s.has_attachment else ""
        lines.append(f"- **{s.message_id}** · {s.received}{status}{clip} · {_format_size(s.size)}")
        lines.append(f"  From: {s.sender} <{s.from_address}>")
        lines.append(f"  Subject: {s.subject}")
        if s.snippet:
            lines.append(f"  Snippet: {s.snippet}")
    return "\n".join(lines)


# --- input models --------------------------------------------------------------


class AccountScoped(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    account: Optional[str] = Field(
        default=None,
        description="Configured account name (see zoho_list_accounts). Omit to use the default account.",
    )


class FolderInput(AccountScoped):
    folder: str = Field(default="Inbox", description="Folder name, e.g. 'Inbox', 'Sent'.")


class ListEmailsInput(FolderInput):
    limit: int = Field(default=20, ge=1, le=100, description="Maximum messages to return (newest first).")
    unread_only: bool = Field(default=False, description="Only unread messages.")
    from_contains: Optional[str] = Field(default=None, description="Filter: sender matches this text (account-wide search).")
    subject_contains: Optional[str] = Field(default=None, description="Filter: subject matches this text (account-wide search).")
    text_contains: Optional[str] = Field(default=None, description="Filter: message content matches this text (account-wide search).")


class GetEmailInput(FolderInput):
    message_id: str = Field(..., pattern=r"^\d+$", description="Message id from zoho_list_emails.")
    mark_read: bool = Field(default=False, description="Also mark the message as read (default leaves it untouched).")


class SaveAttachmentInput(FolderInput):
    message_id: str = Field(..., pattern=r"^\d+$", description="Message id from zoho_list_emails.")
    attachment_index: int = Field(..., ge=0, description="Attachment index from zoho_get_email (0-based).")
    save_dir: str = Field(default="~/Downloads", description="Directory to save into (created if missing).")


class MarkEmailsInput(AccountScoped):
    message_ids: list[str] = Field(..., min_length=1, max_length=50, description="Message ids to update.")
    read: bool = Field(default=True, description="True marks as read, False marks as unread.")

    @field_validator("message_ids")
    @classmethod
    def _check_ids(cls, v: list[str]) -> list[str]:
        for mid in v:
            if not mid.isdigit():
                raise ValueError(f"message id {mid!r} must be numeric")
        return v


class DraftEmailInput(AccountScoped):
    to: list[str] = Field(..., min_length=1, max_length=20, description="Recipient email addresses.")
    subject: str = Field(..., min_length=1, max_length=500, description="Subject line. For replies use 'Re: <original subject>'.")
    body: str = Field(..., min_length=1, description="Plain-text body, sent verbatim.")
    cc: list[str] = Field(default_factory=list, max_length=20, description="Cc addresses.")
    bcc: list[str] = Field(default_factory=list, max_length=20, description="Bcc addresses.")
    reply_to_message_id: Optional[str] = Field(
        default=None, pattern=r"^\d+$",
        description="Message id to reply to; the reply is threaded onto that conversation.",
    )

    @field_validator("to")
    @classmethod
    def _check_to(cls, v: list[str]) -> list[str]:
        return _validate_addresses("to", v)

    @field_validator("cc")
    @classmethod
    def _check_cc(cls, v: list[str]) -> list[str]:
        return _validate_addresses("cc", v)

    @field_validator("bcc")
    @classmethod
    def _check_bcc(cls, v: list[str]) -> list[str]:
        return _validate_addresses("bcc", v)


class SendEmailInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    draft_id: str = Field(..., description="Draft id returned by zoho_draft_email.")
    confirm_token: str = Field(..., description="Confirmation token from the same draft preview.")
    user_approved: bool = Field(
        ...,
        description=(
            "Must be true, and may ONLY be set to true after the human user has seen the "
            "draft preview and explicitly approved sending it. Never set this on your own."
        ),
    )


class DraftIdInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    draft_id: str = Field(..., description="Draft id returned by zoho_draft_email.")


# --- read tools -----------------------------------------------------------------


@mcp.tool(
    name="zoho_list_accounts",
    annotations={
        "title": "List Zoho Mail Accounts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def zoho_list_accounts() -> str:
    """List the configured Zoho Mail accounts (names and addresses — never credentials).

    Use the returned account *names* as the `account` argument of the other tools.

    Returns:
        str: Markdown list of accounts with the default account marked, or an
        error explaining how to create the config file.
    """
    try:
        accounts = load_accounts()
    except Exception as exc:
        return _error(exc)
    lines = ["# Configured Zoho Mail accounts", ""]
    for acct in accounts.values():
        marker = " **(default)**" if acct.default else ""
        display = f" ({acct.display_name})" if acct.display_name else ""
        lines.append(f"- **{acct.name}**{marker}: {acct.email}{display} — api {acct.api_base}")
    return "\n".join(lines)


@mcp.tool(
    name="zoho_list_folders",
    annotations={
        "title": "List Mail Folders",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def zoho_list_folders(params: AccountScoped) -> str:
    """List the mail folders of one account (Inbox, Sent, custom folders, ...).

    Args:
        params (AccountScoped): `account` (optional) — account name; omit for the default.

    Returns:
        str: Markdown list of folder names (usable as the `folder` argument of
        other tools) with unread counts where available.
    """
    try:
        acct = resolve_account(params.account)
        client = ZohoClient(acct)
        folders = await client.folders()
    except Exception as exc:
        return _error(exc)
    lines = [f"# Folders — {acct.email}", ""]
    for entry in folders:
        name = entry.get("folderName", "?")
        unread = entry.get("unreadCount")
        suffix = f" ({unread} unread)" if unread not in (None, 0, "0") else ""
        lines.append(f"- {name}{suffix}")
    return "\n".join(lines)


@mcp.tool(
    name="zoho_list_emails",
    annotations={
        "title": "List / Search Emails",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def zoho_list_emails(params: ListEmailsInput) -> str:
    """List emails in a folder, or search the account, newest first. Marks nothing as read.

    Without text filters this lists the given folder. With from_contains /
    subject_contains / text_contains it uses Zoho search, which is account-wide
    (the folder parameter is then ignored). Use the returned message ids with
    zoho_get_email.

    Args:
        params (ListEmailsInput): account, folder (default Inbox), limit (1-100,
            default 20), unread_only, from_contains, subject_contains, text_contains.

    Returns:
        str: Markdown list — one entry per message with message id, date, read
        state, attachment marker, size, sender, subject and snippet.
    """
    try:
        acct = resolve_account(params.account)
        client = ZohoClient(acct)
        search_key = build_search_key(
            unread_only=params.unread_only,
            from_contains=params.from_contains,
            subject_contains=params.subject_contains,
            text_contains=params.text_contains,
        )
        if search_key and search_key != "newMails":
            summaries = await client.search_messages(search_key, params.limit)
            header = f"# Search results (account-wide) — {acct.email}"
        else:
            folder_id, folder_name = await client.resolve_folder(params.folder)
            summaries = await client.list_messages(folder_id, params.limit, params.unread_only)
            header = f"# {folder_name} — {acct.email}"
        return _format_summaries(header, summaries)
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    name="zoho_get_email",
    annotations={
        "title": "Read Full Email",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def zoho_get_email(params: GetEmailInput) -> str:
    """Fetch one email in full: headers, readable body, and attachment list.

    HTML bodies are converted to readable text (links kept as <url>). Reading
    does not change the unread state unless mark_read=true.

    Args:
        params (GetEmailInput): message_id (required, from zoho_list_emails),
            account, folder (the folder containing the message, default Inbox),
            mark_read.

    Returns:
        str: Markdown document with headers, attachment indices (for
        zoho_save_attachment), and the body (truncated past 20k characters).
    """
    try:
        acct = resolve_account(params.account)
        client = ZohoClient(acct)
        folder_id, folder_name = await client.resolve_folder(params.folder)
        msg = await client.get_message(folder_id, params.message_id)
        if params.mark_read and msg.unread:
            await client.set_read_state([params.message_id], read=True)
    except Exception as exc:
        return _error(exc)

    lines = [
        f"# Message {msg.message_id} — {folder_name} — {acct.email}",
        "",
        f"- From: {msg.sender} <{msg.from_address}>",
        f"- To: {msg.to_address}",
    ]
    if msg.cc_address:
        lines.append(f"- Cc: {msg.cc_address}")
    lines += [
        f"- Date: {msg.received}",
        f"- Subject: {msg.subject}",
        f"- Status: {'unread' if msg.unread and not params.mark_read else 'read'}",
    ]
    if msg.attachments:
        lines.append(f"- Attachments ({len(msg.attachments)}):")
        for a in msg.attachments:
            lines.append(f"  - index {a.index}: {a.name} ({_format_size(a.size)})")
    body = msg.body
    truncated = len(body) > BODY_PREVIEW_LIMIT
    if truncated:
        body = body[:BODY_PREVIEW_LIMIT]
    lines += ["", "--- body ---", "", body]
    if truncated:
        lines.append(f"\n[body truncated at {BODY_PREVIEW_LIMIT} characters]")
    return "\n".join(lines)


@mcp.tool(
    name="zoho_save_attachment",
    annotations={
        "title": "Save Email Attachment",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def zoho_save_attachment(params: SaveAttachmentInput) -> str:
    """Download one attachment of an email to local disk.

    Never overwrites: an existing filename gets a -1/-2 suffix.

    Args:
        params (SaveAttachmentInput): message_id, attachment_index (from
            zoho_get_email), account, folder, save_dir (default ~/Downloads).

    Returns:
        str: The saved file path and size, or an error.
    """
    try:
        acct = resolve_account(params.account)
        client = ZohoClient(acct)
        folder_id, _ = await client.resolve_folder(params.folder)
        msg = await client.get_message(folder_id, params.message_id)
        if params.attachment_index >= len(msg.attachments):
            return (
                f"Error: message {params.message_id} has {len(msg.attachments)} attachment(s); "
                f"index {params.attachment_index} is out of range (indices start at 0)."
            )
        attachment = msg.attachments[params.attachment_index]
        payload = await client.download_attachment(folder_id, params.message_id, attachment.attachment_id)
    except Exception as exc:
        return _error(exc)

    safe_name = Path(attachment.name).name.replace("\x00", "") or f"attachment-{params.attachment_index}"
    directory = Path(params.save_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / safe_name
    stem, suffix = target.stem, target.suffix
    counter = 1
    while target.exists():
        target = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    target.write_bytes(payload)
    return f"Saved attachment to {target} ({_format_size(len(payload))})."


@mcp.tool(
    name="zoho_mark_emails",
    annotations={
        "title": "Mark Emails Read/Unread",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def zoho_mark_emails(params: MarkEmailsInput) -> str:
    """Mark messages as read or unread (the only mailbox mutation this server does).

    Args:
        params (MarkEmailsInput): message_ids (1-50), read (true=read,
            false=unread), account.

    Returns:
        str: Confirmation of how many messages were updated.
    """
    try:
        acct = resolve_account(params.account)
        client = ZohoClient(acct)
        await client.set_read_state(params.message_ids, read=params.read)
    except Exception as exc:
        return _error(exc)
    state = "read" if params.read else "unread"
    return f"Marked {len(params.message_ids)} message(s) as {state} ({acct.email})."


# --- sending (two-step, human-confirmed) ------------------------------------------


@mcp.tool(
    name="zoho_draft_email",
    annotations={
        "title": "Draft Email (step 1 of 2)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def zoho_draft_email(params: DraftEmailInput) -> str:
    """Prepare an outgoing email WITHOUT sending it. Step 1 of the mandatory two-step send flow.

    Nothing leaves the machine: the draft is held in memory for 15 minutes and
    returns a preview plus a draft_id and confirm_token. Sending happens only
    via zoho_send_email after the human user approves the preview.

    For replies, pass reply_to_message_id so Zoho threads the reply onto the
    original conversation, and use a 'Re: ...' subject.

    Args:
        params (DraftEmailInput): to (1-20 addresses), subject, body
            (plain text, sent verbatim), cc, bcc, account, reply_to_message_id.

    Returns:
        str: Full draft preview with draft_id and confirm_token, plus
        instructions to obtain the user's explicit approval before sending.
    """
    try:
        acct = resolve_account(params.account)
    except Exception as exc:
        return _error(exc)

    payload = build_send_payload(
        acct,
        to=params.to,
        subject=params.subject,
        body=params.body,
        cc=params.cc or None,
        bcc=params.bcc or None,
    )
    recipients = ", ".join(params.to + params.cc + params.bcc)
    draft = drafts.store(
        acct.name, recipients, params.subject, payload,
        reply_to_message_id=params.reply_to_message_id,
    )

    sender = f"{acct.display_name} <{acct.email}>" if acct.display_name else acct.email
    lines = [
        f"Draft created: **{draft.draft_id}** (expires in {drafts.DRAFT_TTL_SECONDS // 60} minutes)",
        "",
        f"From: {sender} (account: {acct.name})",
        f"To: {', '.join(params.to)}",
    ]
    if params.cc:
        lines.append(f"Cc: {', '.join(params.cc)}")
    if params.bcc:
        lines.append(f"Bcc: {', '.join(params.bcc)}")
    lines.append(f"Subject: {params.subject}")
    if params.reply_to_message_id:
        lines.append(f"(Reply to message {params.reply_to_message_id}; Zoho threads it automatically.)")
    lines += ["", "--- body ---", "", params.body, "", "---", ""]
    lines += [
        "NOT SENT YET. Show this exact preview to the user and ask whether to send it.",
        f"Only after the user explicitly approves, call zoho_send_email with "
        f'draft_id="{draft.draft_id}", confirm_token="{draft.confirm_token}", user_approved=true.',
        "If the user wants changes, call zoho_discard_draft and create a new draft.",
    ]
    return "\n".join(lines)


@mcp.tool(
    name="zoho_send_email",
    annotations={
        "title": "Send Drafted Email (step 2 of 2)",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def zoho_send_email(params: SendEmailInput) -> str:
    """Actually send a draft created by zoho_draft_email. HUMAN APPROVAL REQUIRED.

    Call this ONLY after the human user has seen the exact draft preview and
    explicitly said to send it. Never set user_approved=true on your own
    initiative, and never guess draft_id or confirm_token. A draft can be sent
    once; it is consumed on success and expires after 15 minutes.

    Args:
        params (SendEmailInput): draft_id, confirm_token (both from the draft
            preview), user_approved (must reflect real, explicit user consent).

    Returns:
        str: Send confirmation with recipients, or an error (the draft
        survives a failed send and can be retried).
    """
    if not params.user_approved:
        return (
            "Error: user_approved is false — not sending. Show the draft preview to the "
            "user and only retry with user_approved=true after they explicitly approve."
        )
    try:
        draft = drafts.take(params.draft_id, params.confirm_token)
    except Exception as exc:
        return _error(exc)
    try:
        acct = resolve_account(draft.account_name)
        client = ZohoClient(acct)
        await client.send(draft.payload, reply_to_message_id=draft.reply_to_message_id)
    except Exception as exc:
        drafts.restore(draft)
        return _error(exc)
    return (
        f"Sent {draft.draft_id} from {acct.email} to {draft.recipients_display} "
        f"(subject: {draft.subject!r})."
    )


@mcp.tool(
    name="zoho_save_to_drafts",
    annotations={
        "title": "Save Draft to Zoho Drafts Folder",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def zoho_save_to_drafts(params: DraftIdInput) -> str:
    """Copy a pending draft into the mailbox's real Drafts folder. Does NOT send.

    Use this when the user wants to see, edit or send the message from the Zoho
    web UI or their phone instead of approving a send here. The in-memory draft
    is left intact, so zoho_send_email still works afterwards with the same
    draft_id and confirm_token.

    Note: calling this twice creates two Zoho drafts.

    Reply drafts are saved standalone rather than threaded. Zoho's save-draft API
    does support threading, but via the RFC `Message-ID` headers (inReplyTo +
    refHeader), and the details endpoint only exposes Zoho's numeric messageId,
    so we have nothing to populate them with. Sending via zoho_send_email still
    threads correctly, because that goes through the reply endpoint instead.

    Args:
        params (DraftIdInput): draft_id from zoho_draft_email.

    Returns:
        str: Confirmation with the new Zoho messageId, or an error.
    """
    try:
        draft = drafts.peek(params.draft_id)
    except Exception as exc:
        return _error(exc)
    try:
        acct = resolve_account(draft.account_name)
        message_id = await ZohoClient(acct).save_draft(draft.payload)
    except Exception as exc:
        return _error(exc)
    note = ""
    if draft.reply_to_message_id:
        note = (
            " Note: saved standalone, not threaded onto the original — send it with "
            "zoho_send_email instead if the thread matters."
        )
    return (
        f"Saved {draft.draft_id} to the Drafts folder of {acct.email} "
        f"(messageId {message_id}). Nothing was sent.{note} The pending draft is still "
        f"available here if you would rather approve a send instead."
    )


@mcp.tool(
    name="zoho_discard_draft",
    annotations={
        "title": "Discard Draft",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def zoho_discard_draft(params: DraftIdInput) -> str:
    """Discard a pending draft so it can never be sent (e.g. after the user rejects it).

    Args:
        params (DraftIdInput): draft_id from zoho_draft_email.

    Returns:
        str: Confirmation, or a note that the draft was already gone.
    """
    if drafts.discard(params.draft_id):
        return f"Draft {params.draft_id} discarded."
    return f"Draft {params.draft_id} was already gone (sent, discarded, or expired)."


@mcp.tool(
    name="zoho_list_drafts",
    annotations={
        "title": "List Pending Drafts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def zoho_list_drafts() -> str:
    """List drafts waiting for confirmation (in-memory, this server session only).

    Returns:
        str: One line per pending draft: draft_id, account, recipients, subject,
        and seconds until expiry. Confirm tokens are NOT repeated here; they
        only appear in the original draft preview.
    """
    pending = drafts.pending()
    if not pending:
        return "No pending drafts."
    lines = ["# Pending drafts", ""]
    for d in pending:
        lines.append(
            f"- **{d.draft_id}** (account {d.account_name}, expires in {drafts.remaining_seconds(d)}s) — "
            f"to {d.recipients_display} — subject {d.subject!r}"
        )
    return "\n".join(lines)


# --- CLI -------------------------------------------------------------------------


async def _check_account(acct) -> list[str]:
    client = ZohoClient(acct)
    account_id = await client.account_id()
    folders = await client.folders()
    return [f"[ok]   {acct.name} ({acct.email}) accountId={account_id}, {len(folders)} folders"]


def _check() -> int:
    import asyncio

    try:
        accounts = load_accounts()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    failures = 0
    for acct in accounts.values():
        try:
            for line in asyncio.run(_check_account(acct)):
                print(line)
        except MailError as exc:
            failures += 1
            print(f"[FAIL] {acct.name} ({acct.email}): {exc}", file=sys.stderr)
    return 1 if failures else 0


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="zoho-mail-mcp", description="Zoho Mail MCP server (stdio).")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("init", help="create the accounts.toml template")
    sub.add_parser("check", help="verify OAuth credentials and API access for every account")
    args = parser.parse_args()

    if args.cmd == "init":
        try:
            path = init_config()
        except ConfigError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        print(f"Created {path} (mode 600). Follow the comments in that file to create")
        print("a Zoho Self Client per mailbox, then run 'zoho-mail-mcp check'.")
    elif args.cmd == "check":
        raise SystemExit(_check())
    else:
        mcp.run()


if __name__ == "__main__":
    main()
