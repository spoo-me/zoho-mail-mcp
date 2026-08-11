# zoho-mail-mcp

MCP server for Zoho Mail. Read, search and (carefully) send email from Claude Code
or any MCP client, across multiple Zoho mailboxes.

Built on the **Zoho Mail REST API** with OAuth 2.0 — not IMAP/SMTP — so it works
on Zoho's free-forever plan, where IMAP/POP are paid features.

## Tools

| Tool | What it does |
| --- | --- |
| `zoho_list_accounts` | Configured accounts (never shows credentials) |
| `zoho_list_folders` | Folders of an account with unread counts |
| `zoho_list_emails` | List a folder or search account-wide (sender/subject/content/unread filters) |
| `zoho_get_email` | Full message: headers, readable body, attachment list |
| `zoho_save_attachment` | Download one attachment to disk |
| `zoho_mark_emails` | Mark read/unread |
| `zoho_draft_email` | Step 1 of sending: build a draft, get a preview + confirm token |
| `zoho_send_email` | Step 2: send a confirmed draft (see safety model below) |
| `zoho_save_to_drafts` | Copy a pending draft into the real Drafts folder, without sending |
| `zoho_discard_draft` / `zoho_list_drafts` | Manage pending drafts |

`zoho_draft_email` holds a draft **in memory only**, so it never appears in the
Zoho web UI. Use `zoho_save_to_drafts` when you'd rather finish or send the
message from Zoho on another device: it writes a real draft via the documented
[Save Draft / Template](https://www.zoho.com/mail/help/api/post-save-draft-template.html)
API (`mode: "draft"`) and leaves the pending draft intact, so approving a send
here still works afterwards.

Reply drafts are saved standalone. Zoho threads drafts via the RFC `Message-ID`
headers (`inReplyTo` / `refHeader`), and the details endpoint only exposes Zoho's
numeric `messageId`, so there is nothing to populate them with. Sending a reply
with `zoho_send_email` still threads correctly, because that uses the reply
endpoint.

## Safety model for sending

Sending is deliberately hard to do by accident:

1. `zoho_draft_email` never sends. It stores the draft in memory (15 min TTL) and
   returns the full preview plus a `draft_id` and a random `confirm_token`.
2. `zoho_send_email` requires the exact `draft_id` + `confirm_token` pair (so the
   model must have created and read the draft), plus `user_approved=true`, which
   the tool description instructs models to set only after a human approved the
   exact preview. Drafts are single-use and expire.
3. Your MCP client's own permission prompt is the final gate. **Do not add
   `zoho_send_email` to any auto-allow list.** Keep it prompting every time.

## Setup

### 1. Install

```bash
cd zoho-mail-mcp
uv sync
```

### 2. Create OAuth credentials (once per mailbox)

IMAP is paid-only on Zoho, but the REST API works with a "Self Client":

1. Log in to the mailbox's Zoho account and open <https://api-console.zoho.com>.
2. **Add Client → Self Client** — note the Client ID and Client Secret.
3. In **Generate Code**, enter the scopes
   `ZohoMail.accounts.READ,ZohoMail.folders.READ,ZohoMail.messages.ALL`,
   pick a duration, and generate the code.
4. Exchange the code for a refresh token *within that duration*:

   ```bash
   curl -s -X POST "https://accounts.zoho.com/oauth/v2/token" \
     -d "grant_type=authorization_code&code=<CODE>&client_id=<ID>&client_secret=<SECRET>"
   ```

   Keep the `refresh_token` from the response (it does not expire).

A Self Client authorizes whichever user is logged in, so repeat this per mailbox.

### 3. Configure accounts

```bash
uv run zoho-mail-mcp init   # creates ~/.config/zoho-mail-mcp/accounts.toml (chmod 600)
```

Fill in one `[accounts.<name>]` table per mailbox (email, client_id,
client_secret, refresh_token; mark one `default = true`). Secrets can also come
from env vars via `client_secret_env` / `refresh_token_env`.

Data centers: defaults target zoho.com (US). For EU/India/etc. set `api_base`
and `accounts_base` (e.g. `https://mail.zoho.eu`, `https://accounts.zoho.eu`).

### 4. Verify and register

```bash
uv run zoho-mail-mcp check   # refreshes tokens, resolves account ids and folders
claude mcp add --scope user zoho-mail -- uv run --directory /path/to/zoho-mail-mcp zoho-mail-mcp
```

## Notes

- Reading never marks mail as read (pass `mark_read=true` explicitly if wanted).
- Marking read/unread is the only mailbox mutation; no delete/move/archive tools exist on purpose.
- Zoho access tokens live ~1 hour and are cached in-process; refresh is automatic.
- Alternative: Zoho's own hosted MCP (<https://www.zoho.com/mcp/>) covers Mail with
  more write-actions, but has no draft-confirmation flow and one server per login.
