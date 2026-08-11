# zoho-mail-mcp

MCP server for Zoho Mail. Read, search and (carefully) send email from Claude Code
or any MCP client, across multiple Zoho mailboxes.

Built on the **Zoho Mail REST API** with OAuth 2.0, not IMAP/SMTP, so it works
on Zoho's free-forever plan, where IMAP/POP are paid features.

## Install

With [uv](https://docs.astral.sh/uv/):

```bash
uvx zoho-mail-mcp setup    # interactive wizard, see below
claude mcp add --scope user zoho-mail -- uvx zoho-mail-mcp
```

Or from source:

```bash
git clone https://github.com/spoo-me/zoho-mail-mcp && cd zoho-mail-mcp
uv sync
uv run zoho-mail-mcp setup
claude mcp add --scope user zoho-mail -- uv run --directory "$PWD" zoho-mail-mcp
```

Any other MCP client works too: run `zoho-mail-mcp` (stdio) as the command.

## Setup

`zoho-mail-mcp setup` walks through connecting one mailbox and is safe to re-run
to add more mailboxes or rotate credentials:

1. Asks for an account name, the mailbox address, and the data center
   (`us`, `eu`, `in`, `au`, `jp`, `ca`, `sa`, `cn`). An account lives in exactly
   one DC; the wizard derives all endpoints from it.
2. Points you at the right `api-console.zoho.*` page to create a **Self Client**
   and paste its Client ID / Client Secret.
3. Exchanges the generated grant code for a permanent refresh token (no curl
   involved) and verifies live API access before writing anything.
4. Saves the account to `~/.config/zoho-mail-mcp/accounts.toml` (mode 600).

A Self Client authorizes whichever user is logged in, so each mailbox needs its
own (repeat the wizard per mailbox).

Prefer doing it by hand? `zoho-mail-mcp init` writes a commented template with
the manual steps. Secrets can come from env vars via `client_secret_env` /
`refresh_token_env`, and `datacenter = "eu"` can be replaced by explicit
`api_base` / `accounts_base` URLs. `zoho-mail-mcp check` re-verifies every
account.

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

## Notes

- Reading never marks mail as read (pass `mark_read=true` explicitly if wanted).
- Marking read/unread is the only mailbox mutation; no delete/move/archive tools exist on purpose.
- Zoho access tokens live about an hour and are cached in-process; refresh is automatic.
- Alternative: Zoho's own hosted MCP (<https://www.zoho.com/mcp/>) covers Mail with
  more write-actions, but has no draft-confirmation flow and needs one server per login.

<!-- mcp-name: io.github.spoo-me/zoho-mail-mcp -->
