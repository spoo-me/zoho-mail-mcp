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

# Zoho data centers: short name -> domain. API, accounts and developer-console
# hosts all derive from the domain (mail.<domain>, accounts.<domain>,
# api-console.<domain>). An account lives in exactly one DC; using the wrong
# one fails with invalid_client at token refresh.
DATACENTERS = {
    "us": "zoho.com",
    "eu": "zoho.eu",
    "in": "zoho.in",
    "au": "zoho.com.au",
    "jp": "zoho.jp",
    "ca": "zohocloud.ca",
    "sa": "zoho.sa",
    "cn": "zoho.com.cn",
}

DEFAULT_API_BASE = "https://mail.zoho.com"
DEFAULT_ACCOUNTS_BASE = "https://accounts.zoho.com"


def datacenter_bases(datacenter: str) -> tuple[str, str]:
    """Datacenter name -> (api_base, accounts_base)."""
    domain = DATACENTERS.get(datacenter.strip().lower())
    if not domain:
        raise ConfigError(
            f"Unknown datacenter {datacenter!r}. Known: {', '.join(DATACENTERS)}."
        )
    return f"https://mail.{domain}", f"https://accounts.{domain}"

OAUTH_SCOPES = "ZohoMail.accounts.READ,ZohoMail.folders.READ,ZohoMail.messages.ALL"

CONFIG_TEMPLATE = f"""\
# zoho-mail-mcp accounts.
# Each [accounts.<name>] table is one mailbox. <name> is the identifier
# passed as the `account` argument to the MCP tools.
#
# Easiest path: run `zoho-mail-mcp setup` instead of editing this file by
# hand. The wizard walks through Self Client creation, exchanges the grant
# code for a refresh token, verifies API access, and writes this file.
#
# Manual path: credentials come from a Zoho "Self Client" (works on free plans, unlike IMAP):
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
# Data centers: set `datacenter` to one of {', '.join(DATACENTERS)} (default
# us). Explicit api_base / accounts_base settings win over `datacenter`.

[accounts.main]
email = "you@example.com"
client_id = ""
client_secret = ""
refresh_token = ""
display_name = ""
default = true
# datacenter = "us"

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
        api_base = str(raw.get("api_base", "")).rstrip("/")
        accounts_base = str(raw.get("accounts_base", "")).rstrip("/")
        if raw.get("datacenter"):
            dc_api, dc_accounts = datacenter_bases(str(raw["datacenter"]))
            api_base = api_base or dc_api
            accounts_base = accounts_base or dc_accounts
        accounts[name] = Account(
            name=name,
            email=email_addr,
            client_id=_resolve_secret(name, raw, "client_id"),
            client_secret=_resolve_secret(name, raw, "client_secret"),
            refresh_token=_resolve_secret(name, raw, "refresh_token"),
            api_base=api_base or DEFAULT_API_BASE,
            accounts_base=accounts_base or DEFAULT_ACCOUNTS_BASE,
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
