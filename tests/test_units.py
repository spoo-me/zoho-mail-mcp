"""Unit tests: config parsing, search-key building, HTML extraction, draft flow."""

import asyncio

import pytest

from zoho_mail_mcp import drafts
from zoho_mail_mcp.config import ConfigError, load_accounts, resolve_account
from zoho_mail_mcp.htmltext import html_to_text
from zoho_mail_mcp.zoho_api import build_search_key, build_send_payload, _is_unread


CONFIG = """\
[accounts.admin]
email = "admin@spoo.me"
client_id = "cid"
client_secret = "csecret"
refresh_token = "rtoken"
display_name = "Zingzy"
default = true

[accounts.support]
email = "support@spoo.me"
client_id = "cid2"
client_secret_env = "TEST_SUPPORT_SECRET"
refresh_token = "rtoken2"
"""


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOHO_MAIL_MCP_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_SUPPORT_SECRET", "envsecret")
    path = tmp_path / "accounts.toml"
    path.write_text(CONFIG)
    path.chmod(0o600)
    return tmp_path


class TestConfig:
    def test_loads_accounts(self, config_dir):
        accounts = load_accounts()
        assert set(accounts) == {"admin", "support"}
        assert accounts["admin"].default is True
        assert accounts["support"].client_secret == "envsecret"

    def test_default_account_resolution(self, config_dir):
        assert resolve_account(None).name == "admin"
        assert resolve_account("support").email == "support@spoo.me"

    def test_unknown_account(self, config_dir):
        with pytest.raises(ConfigError, match="Unknown account"):
            resolve_account("nope")

    def test_rejects_world_readable_config(self, config_dir):
        (config_dir / "accounts.toml").chmod(0o644)
        with pytest.raises(ConfigError, match="chmod 600"):
            load_accounts()

    def test_missing_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ZOHO_MAIL_MCP_CONFIG_DIR", str(tmp_path / "nothing"))
        with pytest.raises(ConfigError, match="init"):
            load_accounts()

    def test_missing_env_secret(self, config_dir, monkeypatch):
        monkeypatch.delenv("TEST_SUPPORT_SECRET")
        with pytest.raises(ConfigError, match="TEST_SUPPORT_SECRET"):
            load_accounts()


class TestSearchKey:
    def test_combined_filters(self):
        key = build_search_key(unread_only=True, from_contains="github", subject_contains="issue")
        assert key == "sender:github::subject:issue::newMails"

    def test_empty(self):
        assert build_search_key() == ""

    def test_unread_flag_status(self):
        assert _is_unread({"status": "0"}) is True
        assert _is_unread({"status": "1"}) is False
        assert _is_unread({}) is False


class TestHtmlToText:
    def test_basic_blocks_and_links(self):
        html = (
            "<html><head><style>body{color:red}</style></head>"
            "<body><h1>Hello</h1><p>Check <a href='https://spoo.me/x'>this link</a> now.</p>"
            "<ul><li>one</li><li>two</li></ul></body></html>"
        )
        text = html_to_text(html)
        assert "color:red" not in text
        assert "Hello" in text
        assert "this link <https://spoo.me/x>" in text
        assert "- one" in text and "- two" in text

    def test_empty(self):
        assert html_to_text("") == "(empty HTML body)"


class TestDraftFlow:
    def _make_draft(self):
        payload = {"fromAddress": "admin@spoo.me", "toAddress": "a@b.co", "subject": "s", "content": "c"}
        return drafts.store("admin", "a@b.co", "s", payload)

    def test_take_requires_exact_token(self):
        draft = self._make_draft()
        with pytest.raises(drafts.DraftError, match="confirm_token does not match"):
            drafts.take(draft.draft_id, "WRONG")
        taken = drafts.take(draft.draft_id, draft.confirm_token)
        assert taken.payload["toAddress"] == "a@b.co"

    def test_single_use(self):
        draft = self._make_draft()
        drafts.take(draft.draft_id, draft.confirm_token)
        with pytest.raises(drafts.DraftError, match="not found or expired"):
            drafts.take(draft.draft_id, draft.confirm_token)

    def test_restore_after_failed_send(self):
        draft = self._make_draft()
        taken = drafts.take(draft.draft_id, draft.confirm_token)
        drafts.restore(taken)
        assert drafts.take(draft.draft_id, draft.confirm_token) is taken

    def test_discard(self):
        draft = self._make_draft()
        assert drafts.discard(draft.draft_id) is True
        assert drafts.discard(draft.draft_id) is False

    def test_peek_does_not_consume(self):
        draft = self._make_draft()
        assert drafts.peek(draft.draft_id) is draft
        assert drafts.take(draft.draft_id, draft.confirm_token) is draft

    def test_peek_expired(self, monkeypatch):
        draft = self._make_draft()
        monkeypatch.setattr(drafts, "DRAFT_TTL_SECONDS", -1)
        with pytest.raises(drafts.DraftError, match="not found or expired"):
            drafts.peek(draft.draft_id)

    def test_expiry(self, monkeypatch):
        draft = self._make_draft()
        monkeypatch.setattr(drafts, "DRAFT_TTL_SECONDS", -1)
        with pytest.raises(drafts.DraftError, match="not found or expired"):
            drafts.take(draft.draft_id, draft.confirm_token)


class TestSendPayload:
    def test_payload_shape(self, config_dir):
        acct = resolve_account("admin")
        payload = build_send_payload(acct, to=["x@y.co", "z@y.co"], subject="Hi", body="Body", cc=["c@y.co"])
        assert payload == {
            "fromAddress": "admin@spoo.me",
            "toAddress": "x@y.co,z@y.co",
            "ccAddress": "c@y.co",
            "subject": "Hi",
            "content": "Body",
            "mailFormat": "plaintext",
        }


class TestServerSurface:
    def test_tools_registered(self):
        from zoho_mail_mcp.server import mcp

        tools = asyncio.run(mcp.list_tools())
        names = {t.name for t in tools}
        assert names == {
            "zoho_list_accounts",
            "zoho_list_folders",
            "zoho_list_emails",
            "zoho_get_email",
            "zoho_save_attachment",
            "zoho_mark_emails",
            "zoho_save_to_drafts",
            "zoho_draft_email",
            "zoho_send_email",
            "zoho_discard_draft",
            "zoho_list_drafts",
        }
        send = next(t for t in tools if t.name == "zoho_send_email")
        assert send.annotations.destructiveHint is True
        assert "HUMAN APPROVAL" in send.description

    def test_send_refuses_without_user_approval(self):
        from zoho_mail_mcp.server import SendEmailInput, zoho_send_email

        result = asyncio.run(
            zoho_send_email(SendEmailInput(draft_id="draft-x", confirm_token="T", user_approved=False))
        )
        assert result.startswith("Error: user_approved is false")

    def test_send_rejects_unknown_draft(self):
        from zoho_mail_mcp.server import SendEmailInput, zoho_send_email

        result = asyncio.run(
            zoho_send_email(SendEmailInput(draft_id="draft-missing", confirm_token="T", user_approved=True))
        )
        assert "not found or expired" in result


class TestDatacenters:
    def test_known_datacenter(self):
        from zoho_mail_mcp.config import datacenter_bases

        assert datacenter_bases("in") == ("https://mail.zoho.in", "https://accounts.zoho.in")
        assert datacenter_bases(" EU ") == ("https://mail.zoho.eu", "https://accounts.zoho.eu")

    def test_unknown_datacenter(self):
        from zoho_mail_mcp.config import datacenter_bases

        with pytest.raises(ConfigError, match="Unknown datacenter"):
            datacenter_bases("mars")

    def test_config_datacenter_field(self, config_dir):
        path = config_dir / "accounts.toml"
        path.write_text(CONFIG.replace('default = true\n', 'default = true\ndatacenter = "in"\n'))
        acct = load_accounts()["admin"]
        assert acct.api_base == "https://mail.zoho.in"
        assert acct.accounts_base == "https://accounts.zoho.in"

    def test_explicit_base_wins_over_datacenter(self, config_dir):
        path = config_dir / "accounts.toml"
        path.write_text(
            CONFIG.replace(
                'default = true\n',
                'default = true\ndatacenter = "in"\napi_base = "https://mail.example.test"\n',
            )
        )
        acct = load_accounts()["admin"]
        assert acct.api_base == "https://mail.example.test"
        assert acct.accounts_base == "https://accounts.zoho.in"


class TestSetupWizardHelpers:
    def test_render_block_escapes_strings(self):
        from zoho_mail_mcp.setup_wizard import render_account_block

        block = render_account_block("main", {"email": 'a"b@x.co', "default": True})
        assert block == '[accounts.main]\nemail = "a\\"b@x.co"\ndefault = true\n'

    def test_upsert_appends_new_account(self):
        import tomllib

        from zoho_mail_mcp.setup_wizard import render_account_block, upsert_account_block

        block = render_account_block("support", {"email": "s@x.co", "refresh_token": "r"})
        text = upsert_account_block(CONFIG, "support2", block.replace("support", "support2"))
        data = tomllib.loads(text)
        assert set(data["accounts"]) == {"admin", "support", "support2"}
        assert data["accounts"]["admin"]["client_id"] == "cid"

    def test_upsert_replaces_existing_account(self):
        import tomllib

        from zoho_mail_mcp.setup_wizard import render_account_block, upsert_account_block

        block = render_account_block("admin", {"email": "admin@spoo.me", "refresh_token": "NEW"})
        text = upsert_account_block(CONFIG, "admin", block)
        data = tomllib.loads(text)
        assert set(data["accounts"]) == {"admin", "support"}
        assert data["accounts"]["admin"]["refresh_token"] == "NEW"
        assert "display_name" not in data["accounts"]["admin"]
        assert data["accounts"]["support"]["refresh_token"] == "rtoken2"

    def test_upsert_into_empty_file(self):
        import tomllib

        from zoho_mail_mcp.setup_wizard import render_account_block, upsert_account_block

        text = upsert_account_block("", "main", render_account_block("main", {"email": "m@x.co"}))
        assert tomllib.loads(text)["accounts"]["main"]["email"] == "m@x.co"

    def test_exchange_grant_code_paths(self, monkeypatch):
        import httpx

        from zoho_mail_mcp import setup_wizard

        class FakeResponse:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        calls = {}

        def fake_post(url, data=None, timeout=None):
            calls["url"] = url
            calls["data"] = data
            return FakeResponse({"refresh_token": "rt", "access_token": "at"})

        monkeypatch.setattr(httpx, "post", fake_post)
        token = setup_wizard.exchange_grant_code("https://accounts.zoho.in", "id", "sec", "code1")
        assert token == "rt"
        assert calls["url"] == "https://accounts.zoho.in/oauth/v2/token"
        assert calls["data"]["grant_type"] == "authorization_code"

        monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse({"error": "invalid_code"}))
        with pytest.raises(setup_wizard.SetupError, match="expire within minutes"):
            setup_wizard.exchange_grant_code("https://accounts.zoho.in", "id", "sec", "old")

    def test_guess_datacenter(self, config_dir):
        from zoho_mail_mcp.setup_wizard import guess_datacenter

        path = config_dir / "accounts.toml"
        path.write_text(CONFIG.replace('default = true\n', 'default = true\ndatacenter = "in"\n'))
        assert guess_datacenter(load_accounts()) == "in"
        assert guess_datacenter({}) == "us"
