"""Account configuration: ~/.config/zoho-mail-mcp/accounts.toml.

Uses the Zoho Mail REST API (OAuth 2.0), NOT IMAP/SMTP — IMAP is a paid
feature on Zoho Mail, while the REST API also works on free-plan mailboxes.
Each account needs a client_id / client_secret / refresh_token from a Zoho
"Self Client" (api-console.zoho.com); see the template below and README.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

# US data center. EU accounts use mail.zoho.eu / accounts.zoho.eu, India .in, etc.
DEFAULT_API_BASE = "https://mail.zoho.com"
DEFAULT_ACCOUNTS_BASE = "https://accounts.zoho.com"

OAUTH_SCOPES = "ZohoMail.accounts.READ,ZohoMail.folders.READ,ZohoMail.messages.ALL"

CONFIG_TEMPLATE = f"""\
# zoho-mail-mcp accounts.
# Each [accounts.<name>] table is one mailbox. <name> is the identifier
# passed as the `account` argument to the MCP tools.
#
# Credentials come from a Zoho "Self Client" (works on free plans, unlike IMAP):
#   1. Log in to the mailbox's Zoho account, open https://api-console.zoho.com
#   2. Add Client -> Self Client -> note the Client ID and Client Secret.
#   3. In the Generate Code tab, enter scopes:
#        {OAUTH_SCOPES}
#      choose a duration, and generate the code.
#   4. Exchange the code for a refresh token (within the duration!):
#        curl -s -X POST "https://accounts.zoho.com/oauth/v2/token" \\
#          -d "grant_type=authorization_code&code=<CODE>&client_id=<ID>&client_secret=<SECRET>"
#      and copy the "refresh_token" from the response here.
#   Repeat per mailbox (a Self Client authorizes the logged-in user only).
#
# Any of client_id / client_secret / refresh_token may instead be given as
# <field>_env = "ENV_VAR_NAME".
#
# Data centers: default hosts are the US DC (zoho.com). For zoho.eu / zoho.in
# etc., set api_base and accounts_base accordingly.

[accounts.main]
email = "you@example.com"
client_id = ""
client_secret = ""
refresh_token = ""
display_name = ""
default = true
# api_base = "{DEFAULT_API_BASE}"
# accounts_base = "{DEFAULT_ACCOUNTS_BASE}"

# [accounts.support]
# email = "support@example.com"
# client_id = ""
# client_secret_env = "ZOHO_SUPPORT_CLIENT_SECRET"
# refresh_token_env = "ZOHO_SUPPORT_REFRESH_TOKEN"
"""


class ConfigError(RuntimeError):
    """Raised when the accounts file is missing or invalid."""


@dataclass(frozen=True)
class Account:
    name: str
    email: str
    client_id: str
    client_secret: str
    refresh_token: str
    api_base: str = DEFAULT_API_BASE
    accounts_base: str = DEFAULT_ACCOUNTS_BASE
    display_name: str = ""
    default: bool = False


def config_path() -> Path:
    base = os.environ.get("ZOHO_MAIL_MCP_CONFIG_DIR", "~/.config/zoho-mail-mcp")
    return Path(base).expanduser() / "accounts.toml"


def _resolve_secret(account_name: str, raw: dict, field: str) -> str:
    env_name = raw.get(f"{field}_env")
    if env_name:
        value = os.environ.get(env_name, "")
        if not value:
            raise ConfigError(
                f"Account '{account_name}': environment variable {env_name!r} "
                f"({field}_env) is not set."
            )
        return value
    value = raw.get(field, "")
    if not value:
        raise ConfigError(
            f"Account '{account_name}': set '{field}' (or '{field}_env') in {config_path()}. "
            "See the comments in that file for how to create a Zoho Self Client."
        )
    return value


def load_accounts() -> dict[str, Account]:
    path = config_path()
    if not path.exists():
        raise ConfigError(
            f"No config found at {path}. Run 'zoho-mail-mcp init' to create a template, "
            "then fill in the Zoho OAuth credentials."
        )
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(
            f"{path} is readable by other users (mode {oct(mode)}); it holds mail "
            f"credentials. Fix with: chmod 600 {path}"
        )
    with path.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    raw_accounts = data.get("accounts")
    if not isinstance(raw_accounts, dict) or not raw_accounts:
        raise ConfigError(f"{path} defines no [accounts.<name>] tables.")

    accounts: dict[str, Account] = {}
    for name, raw in raw_accounts.items():
        if not isinstance(raw, dict):
            raise ConfigError(f"[accounts.{name}] must be a table.")
        email_addr = raw.get("email", "")
        if "@" not in email_addr:
            raise ConfigError(f"Account '{name}': 'email' is missing or invalid.")
        accounts[name] = Account(
            name=name,
            email=email_addr,
            client_id=_resolve_secret(name, raw, "client_id"),
            client_secret=_resolve_secret(name, raw, "client_secret"),
            refresh_token=_resolve_secret(name, raw, "refresh_token"),
            api_base=raw.get("api_base", DEFAULT_API_BASE).rstrip("/"),
            accounts_base=raw.get("accounts_base", DEFAULT_ACCOUNTS_BASE).rstrip("/"),
            display_name=raw.get("display_name", ""),
            default=bool(raw.get("default", False)),
        )
    return accounts


def resolve_account(name: str | None) -> Account:
    """Pick an account by name, falling back to the default/only account."""
    accounts = load_accounts()
    if name:
        try:
            return accounts[name]
        except KeyError:
            raise ConfigError(
                f"Unknown account '{name}'. Configured accounts: {', '.join(sorted(accounts))}"
            ) from None
    defaults = [a for a in accounts.values() if a.default]
    if len(defaults) == 1:
        return defaults[0]
    if len(accounts) == 1:
        return next(iter(accounts.values()))
    raise ConfigError(
        "Multiple accounts are configured and none (or several) are marked "
        f"'default = true'. Pass an explicit account name: {', '.join(sorted(accounts))}"
    )


def init_config() -> Path:
    path = config_path()
    if path.exists():
        raise ConfigError(f"{path} already exists; not overwriting.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE)
    path.chmod(0o600)
    return path
