"""Interactive `zoho-mail-mcp setup` wizard.

Walks through creating a Zoho Self Client, exchanges the grant code for a
refresh token (the step people otherwise do with curl), verifies live API
access, and writes the account into accounts.toml. Re-run it any time to add
another mailbox or rotate the credentials of an existing one.
"""

from __future__ import annotations

import getpass
import json
import re
import sys
import tomllib
from pathlib import Path

import httpx

from .config import (
    DATACENTERS,
    OAUTH_SCOPES,
    Account,
    ConfigError,
    config_path,
    datacenter_bases,
    load_accounts,
)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

CONFIG_HEADER = """\
# zoho-mail-mcp accounts. Managed by `zoho-mail-mcp setup`; hand-editing is
# fine too (see `zoho-mail-mcp init` for a commented template).
"""


class SetupError(RuntimeError):
    """Setup failed; the message is safe to show the user."""


# --- pure helpers (unit-tested) ------------------------------------------------


def exchange_grant_code(
    accounts_base: str, client_id: str, client_secret: str, code: str
) -> str:
    """Trade a Self Client grant code for a permanent refresh token."""
    try:
        resp = httpx.post(
            f"{accounts_base}/oauth/v2/token",
            data={  # body, not query params: keeps secrets out of URL logs
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise SetupError(f"Could not reach {accounts_base}: {exc}") from exc
    try:
        data = resp.json()
    except ValueError:
        raise SetupError(
            f"Token endpoint returned non-JSON (HTTP {resp.status_code}). "
            "Wrong data center?"
        ) from None
    token = data.get("refresh_token")
    if not token:
        error = data.get("error", data)
        hint = ""
        if "invalid_code" in str(error):
            hint = (
                " Grant codes expire within minutes; generate a fresh one and "
                "paste it right away."
            )
        elif "invalid_client" in str(error):
            hint = (
                " invalid_client usually means the wrong data center: the account "
                "lives in exactly one DC and its token endpoint is DC-specific."
            )
        raise SetupError(f"Token exchange failed: {error}.{hint}")
    return str(token)


def render_account_block(name: str, fields: dict) -> str:
    """Render one [accounts.<name>] TOML table. Values are str or bool."""
    lines = [f"[accounts.{name}]"]
    for key, value in fields.items():
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        else:
            # json.dumps produces a valid TOML basic string for any input.
            lines.append(f"{key} = {json.dumps(str(value))}")
    return "\n".join(lines) + "\n"


def upsert_account_block(text: str, name: str, block: str) -> str:
    """Insert or replace one account table in the config file text."""
    pattern = re.compile(
        rf"(?ms)^\[accounts\.{re.escape(name)}\][ \t]*\n.*?(?=^\[|\Z)"
    )
    if pattern.search(text):
        new_text = pattern.sub(block + "\n", text, count=1)
    else:
        new_text = text
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        if new_text:
            new_text += "\n"
        new_text += block
    tomllib.loads(new_text)  # refuse to write a file that no longer parses
    return new_text


def write_account(name: str, fields: dict) -> Path:
    path = config_path()
    text = path.read_text() if path.exists() else CONFIG_HEADER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(upsert_account_block(text, name, render_account_block(name, fields)))
    path.chmod(0o600)
    return path


def guess_datacenter(accounts: dict[str, Account]) -> str:
    """Default the DC prompt to whatever the existing accounts use."""
    for acct in accounts.values():
        for dc in DATACENTERS:
            if acct.api_base == datacenter_bases(dc)[0]:
                return dc
    return "us"


# --- interactive flow ------------------------------------------------------------


def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    reader = getpass.getpass if secret else input
    while True:
        value = reader(f"{label}{suffix}: ").strip()
        if not value and default:
            return default
        if value:
            return value
        print("  A value is required.")


def _confirm(question: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    value = input(f"{question} {suffix} ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes")


def run_wizard() -> int:
    if not sys.stdin.isatty():
        print("Error: setup is interactive; run it in a terminal.", file=sys.stderr)
        return 1

    try:
        existing = load_accounts()
    except ConfigError:
        existing = {}

    print("zoho-mail-mcp setup: connect a Zoho mailbox over the REST API.")
    print("(Works on free-plan mailboxes; IMAP is not used.)\n")
    if existing:
        print(f"Configured accounts: {', '.join(sorted(existing))}")
        print("Enter an existing name to rotate its credentials, or a new one to add.\n")

    # 1. Account identity.
    default_name = "" if existing else "main"
    while True:
        name = _prompt("Account name (used as the `account` tool argument)", default_name)
        if _NAME_RE.match(name):
            break
        print("  Use lowercase letters, digits, - or _ (must start alphanumeric).")
    if name in existing and not _confirm(
        f"'{name}' already exists ({existing[name].email}). Overwrite its credentials?"
    ):
        print("Aborted; nothing written.")
        return 1
    email = _prompt("Mailbox address", existing[name].email if name in existing else "")
    if "@" not in email:
        print("Error: that is not an email address.", file=sys.stderr)
        return 1

    # 2. Data center.
    print("\nZoho data centers (an account lives in exactly one):")
    for dc, domain in DATACENTERS.items():
        print(f"  {dc:3} {domain}")
    while True:
        datacenter = _prompt("Data center", guess_datacenter(existing)).lower()
        if datacenter in DATACENTERS:
            break
        print(f"  Pick one of: {', '.join(DATACENTERS)}")
    api_base, accounts_base = datacenter_bases(datacenter)
    console = f"https://api-console.{DATACENTERS[datacenter]}"

    # 3. Self Client credentials.
    print(f"\nIn a browser, logged in as {email}:")
    print(f"  1. Open {console}")
    print("  2. Add Client -> Self Client (skip if you already have one).")
    print("  3. Copy the Client ID and Client Secret below.\n")
    client_id = _prompt("Client ID")
    client_secret = _prompt("Client Secret", secret=True)

    # 4. Grant code -> refresh token.
    print("\nStill in the console, open the Generate Code tab:")
    print("  1. Scope (paste exactly):")
    print(f"       {OAUTH_SCOPES}")
    print("  2. Pick any duration and generate the code.")
    print("  3. Paste it below before the duration runs out.\n")
    code = _prompt("Grant code")
    try:
        refresh_token = exchange_grant_code(accounts_base, client_id, client_secret, code)
    except SetupError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    print("Refresh token obtained (it does not expire).")

    display_name = input("\nDisplay name for outgoing mail (optional): ").strip()

    # 5. Verify live access before persisting anything.
    others = {n: a for n, a in existing.items() if n != name}
    make_default = not others or (
        not any(a.default for a in others.values())
        and _confirm("Mark this account as the default?", default=True)
    )
    acct = Account(
        name=name,
        email=email,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        api_base=api_base,
        accounts_base=accounts_base,
        display_name=display_name,
        default=make_default,
    )
    print("\nVerifying API access...")
    import asyncio

    from .zoho_api import MailError, ZohoClient

    try:
        client = ZohoClient(acct)
        asyncio.run(client.account_id())
        folders = asyncio.run(client.folders())
    except MailError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        print("Nothing was written; fix the issue and re-run setup.", file=sys.stderr)
        return 1
    print(f"OK: {email} resolved with {len(folders)} folders.")

    # 6. Persist.
    fields: dict = {
        "email": email,
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }
    if display_name:
        fields["display_name"] = display_name
    fields["default"] = make_default
    fields["datacenter"] = datacenter
    path = write_account(name, fields)

    print(f"\nSaved account '{name}' to {path} (mode 600).")
    print("\nNext steps:")
    print("  zoho-mail-mcp check                # re-verify all accounts any time")
    print("  claude mcp add --scope user zoho-mail -- uvx zoho-mail-mcp")
    print("Re-run `zoho-mail-mcp setup` to add another mailbox.")
    return 0
