import base64
import hashlib
import io
import json
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.hashers import check_password, make_password
from django.core.management import CommandError, call_command
from django.test import Client, override_settings
from django.utils import timezone

from vault.models import AccessLog, OAuthToken, PendingChange

from .test_api import READ_KEY, SYNC_KEY, ApiTestCase
from .test_writes import KEYS, WRITE_KEY

PASSWORD = "correct horse battery staple"
BASE = "https://vault.example.com"
RESOURCE = BASE + "/api/vault/mcp"
CHATGPT_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()

OAUTH_SETTINGS = {
    "VAULT_PUBLIC_URL": BASE,
    "VAULT_OWNER_PASSWORD_HASH": make_password(PASSWORD, hasher="md5"),
    "VAULT_OAUTH_MAX_FAILED_LOGINS": 3,
    **KEYS,
}


@override_settings(**OAUTH_SETTINGS)
class OAuthFlowTestCase(ApiTestCase):
    def setUp(self):
        self.sync([self.note("02_Ideas/料金案.md", "TOMONIの料金案\n")])

    def register(self, redirect=CHATGPT_REDIRECT, name="ChatGPT"):
        response = self.client.post(
            "/oauth/register",
            data=json.dumps({"client_name": name, "redirect_uris": [redirect], "token_endpoint_auth_method": "client_secret_post"}),
            content_type="application/json",
        )
        return response

    def auth_params(self, client_id, **overrides):
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CHATGPT_REDIRECT,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": "st4te",
            "scope": "vault:read vault:write",
            "resource": RESOURCE,
        }
        params.update(overrides)
        return {k: v for k, v in params.items() if v is not None}

    def approve(self, client_id, allow_write=True, password=PASSWORD, **overrides):
        data = {**self.auth_params(client_id, **overrides), "password": password, "decision": "allow"}
        if allow_write:
            data["allow_write"] = "1"
        return self.client.post("/oauth/authorize", data)

    def code_from(self, response):
        self.assertEqual(response.status_code, 302, response.content[:500])
        query = parse_qs(urlparse(response["Location"]).query)
        return query

    def exchange(self, client_id, code, verifier=VERIFIER):
        return self.client.post(
            "/oauth/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CHATGPT_REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
                "resource": RESOURCE,
            },
        )

    def connect(self, allow_write=True):
        client_id = self.register().json()["client_id"]
        code = self.code_from(self.approve(client_id, allow_write=allow_write))["code"][0]
        tokens = self.exchange(client_id, code).json()
        return client_id, tokens

    def mcp(self, token, method, params=None, message_id=1):
        body = {"jsonrpc": "2.0", "method": method}
        if message_id is not None:
            body["id"] = message_id
        if params is not None:
            body["params"] = params
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post("/api/vault/mcp", data=json.dumps(body), content_type="application/json", **headers)


class DiscoveryAndRegistrationTests(OAuthFlowTestCase):
    def test_metadata(self):
        resource = self.client.get("/.well-known/oauth-protected-resource/api/vault/mcp").json()
        self.assertEqual(resource["resource"], RESOURCE)
        self.assertEqual(resource["authorization_servers"], [BASE])
        self.assertEqual(self.client.get("/.well-known/oauth-protected-resource").json()["resource"], RESOURCE)
        server = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(server["issuer"], BASE)
        self.assertEqual(server["authorization_endpoint"], BASE + "/oauth/authorize")
        self.assertEqual(server["token_endpoint"], BASE + "/oauth/token")
        self.assertEqual(server["registration_endpoint"], BASE + "/oauth/register")
        self.assertEqual(server["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(server["token_endpoint_auth_methods_supported"], ["none"])
        self.assertTrue(server["authorization_response_iss_parameter_supported"])

    def test_registration(self):
        body = self.register().json()
        self.assertEqual(body["token_endpoint_auth_method"], "none")
        self.assertEqual(body["redirect_uris"], [CHATGPT_REDIRECT])
        self.assertEqual(self.register("https://claude.ai/api/mcp/auth_callback", "Claude").status_code, 201)
        self.assertEqual(self.register("http://localhost:3118/callback", "Claude Code").status_code, 201)
        for bad in ["https://evil.example.com/cb", "http://chatgpt.com/cb", "https://chatgpt.com.evil.io/cb", "javascript:alert(1)"]:
            with self.subTest(bad=bad):
                response = self.register(bad)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "invalid_redirect_uri")


class AuthorizeTests(OAuthFlowTestCase):
    def test_consent_page(self):
        client_id = self.register().json()["client_id"]
        response = self.client.get("/oauth/authorize", self.auth_params(client_id))
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn("ChatGPT", page)
        self.assertIn("chatgpt.com", page)
        self.assertIn("書き込みも許可する", page)
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_approve_returns_code_state_and_issuer(self):
        client_id = self.register().json()["client_id"]
        query = self.code_from(self.approve(client_id))
        self.assertEqual(query["state"], ["st4te"])
        self.assertEqual(query["iss"], [BASE])
        self.assertTrue(query["code"][0])

    def test_deny(self):
        client_id = self.register().json()["client_id"]
        data = {**self.auth_params(client_id), "decision": "deny"}
        query = self.code_from(self.client.post("/oauth/authorize", data))
        self.assertEqual(query["error"], ["access_denied"])

    def test_wrong_password_and_lockout(self):
        client_id = self.register().json()["client_id"]
        for _ in range(3):
            response = self.approve(client_id, password="wrong")
            self.assertEqual(response.status_code, 200)
            self.assertIn("パスワードが違います", response.content.decode())
        locked = self.approve(client_id)  # right password, but locked now
        self.assertEqual(locked.status_code, 200)
        self.assertIn("ロック", locked.content.decode())
        self.assertEqual(AccessLog.objects.filter(action="oauth_login", success=False).count(), 3)

    @override_settings(VAULT_OWNER_PASSWORD_HASH="", **{k: v for k, v in OAUTH_SETTINGS.items() if k != "VAULT_OWNER_PASSWORD_HASH"})
    def test_disabled_without_password_hash(self):
        client_id = self.register().json()["client_id"]
        response = self.approve(client_id)
        self.assertEqual(response.status_code, 200)
        self.assertIn("パスワードが設定されていません", response.content.decode())

    def test_invalid_requests(self):
        client_id = self.register().json()["client_id"]
        # Not redirectable: unknown client or mismatched redirect -> error page, never a redirect.
        self.assertEqual(self.client.get("/oauth/authorize", self.auth_params("nope")).status_code, 400)
        response = self.client.get("/oauth/authorize", self.auth_params(client_id, redirect_uri="https://claude.ai/api/mcp/auth_callback"))
        self.assertEqual(response.status_code, 400)
        # Redirectable errors go back to the client.
        for overrides, error in [
            ({"code_challenge_method": "plain"}, "invalid_request"),
            ({"code_challenge": None}, "invalid_request"),
            ({"response_type": "token"}, "unsupported_response_type"),
            ({"resource": "https://other.example.com/mcp"}, "invalid_target"),
        ]:
            with self.subTest(overrides=overrides):
                query = self.code_from(self.client.get("/oauth/authorize", self.auth_params(client_id, **overrides)))
                self.assertEqual(query["error"], [error])

    def test_csrf_is_enforced_on_consent(self):
        browser = Client(enforce_csrf_checks=True)
        client_id = self.register().json()["client_id"]
        page = browser.get("/oauth/authorize", self.auth_params(client_id)).content.decode()
        token = page.split('name="csrfmiddlewaretoken" value="')[1].split('"')[0]
        data = {**self.auth_params(client_id), "password": PASSWORD, "decision": "allow"}
        self.assertEqual(browser.post("/oauth/authorize", data).status_code, 403)
        self.assertEqual(browser.post("/oauth/authorize", {**data, "csrfmiddlewaretoken": token}).status_code, 302)


class TokenTests(OAuthFlowTestCase):
    def test_exchange_and_refresh_rotation(self):
        client_id, tokens = self.connect()
        self.assertEqual(tokens["token_type"], "Bearer")
        self.assertEqual(tokens["scope"], "vault:read vault:write")
        self.assertEqual(tokens["expires_in"], 3600)

        refreshed = self.client.post("/oauth/token", {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
        self.assertEqual(refreshed.status_code, 200)
        new = refreshed.json()
        self.assertNotEqual(new["access_token"], tokens["access_token"])
        # The old pair stops working.
        reuse = self.client.post("/oauth/token", {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
        self.assertEqual(reuse.json()["error"], "invalid_grant")
        self.assertEqual(self.mcp(tokens["access_token"], "ping").status_code, 401)
        self.assertEqual(self.mcp(new["access_token"], "ping").status_code, 200)

    def test_code_is_single_use_and_pkce_checked(self):
        client_id = self.register().json()["client_id"]
        code = self.code_from(self.approve(client_id))["code"][0]
        self.assertEqual(self.exchange(client_id, code, verifier="x" * 64).json()["error"], "invalid_grant")
        # A failed attempt burns the code.
        self.assertEqual(self.exchange(client_id, code).json()["error"], "invalid_grant")

        code = self.code_from(self.approve(client_id))["code"][0]
        self.assertEqual(self.exchange(client_id, code).status_code, 200)
        self.assertEqual(self.exchange(client_id, code).json()["error"], "invalid_grant")

    def test_other_client_cannot_use_code(self):
        client_id = self.register().json()["client_id"]
        other = self.register().json()["client_id"]
        code = self.code_from(self.approve(client_id))["code"][0]
        self.assertEqual(self.exchange(other, code).json()["error"], "invalid_grant")

    def test_expired_code(self):
        client_id = self.register().json()["client_id"]
        code = self.code_from(self.approve(client_id))["code"][0]
        with mock.patch("vault.oauth.timezone.now", return_value=timezone.now() + timedelta(minutes=10)):
            self.assertEqual(self.exchange(client_id, code).json()["error"], "invalid_grant")

    def test_unsupported_grant(self):
        response = self.client.post("/oauth/token", {"grant_type": "client_credentials"})
        self.assertEqual(response.json()["error"], "unsupported_grant_type")

    def test_secrets_never_logged(self):
        _, tokens = self.connect()
        for log in AccessLog.objects.all():
            for value in (log.path, log.detail, log.client):
                self.assertNotIn(tokens["access_token"], value)
                self.assertNotIn(tokens["refresh_token"], value)
                self.assertNotIn(PASSWORD, value)
        stored = OAuthToken.objects.get()
        self.assertNotIn(tokens["access_token"], (stored.access_hash, stored.refresh_hash))


class McpTests(OAuthFlowTestCase):
    def test_requires_token_with_discovery_header(self):
        response = self.mcp(None, "initialize")
        self.assertEqual(response.status_code, 401)
        self.assertIn(f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/api/vault/mcp"', response["WWW-Authenticate"])
        self.assertEqual(self.mcp("bogus-token", "initialize").status_code, 401)

    def test_initialize_list_and_call(self):
        _, tokens = self.connect()
        token = tokens["access_token"]
        init = self.mcp(token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}).json()
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("WorkVault", init["result"]["instructions"])
        self.assertEqual(self.mcp(token, "notifications/initialized", message_id=None).status_code, 202)

        tools = self.mcp(token, "tools/list").json()["result"]["tools"]
        self.assertEqual(
            [t["name"] for t in tools],
            ["search_notes", "read_note", "list_notes", "get_recent_notes", "create_note", "append_note", "update_note"],
        )
        self.assertTrue(tools[0]["annotations"]["readOnlyHint"])

        found = self.mcp(token, "tools/call", {"name": "search_notes", "arguments": {"query": "料金案"}}).json()["result"]
        self.assertFalse(found["isError"])
        self.assertEqual(found["structuredContent"]["results"][0]["path"], "02_Ideas/料金案.md")
        self.assertIn("料金案", found["content"][0]["text"])

        read = self.mcp(token, "tools/call", {"name": "read_note", "arguments": {"path": "02_Ideas/料金案.md"}}).json()["result"]
        self.assertEqual(read["structuredContent"]["content"], "TOMONIの料金案\n")

        created = self.mcp(
            token, "tools/call",
            {"name": "create_note", "arguments": {"folder": "04_AI-Chats", "title": "2026-10-07_料金相談", "content": "要約"}},
        ).json()["result"]
        self.assertFalse(created["isError"], created)
        change = PendingChange.objects.get()
        self.assertEqual((change.path, change.client), ("04_AI-Chats/2026-10-07_料金相談.md", "oauth:ChatGPT"))

    def test_read_only_grant_hides_and_blocks_writes(self):
        _, tokens = self.connect(allow_write=False)
        self.assertEqual(tokens["scope"], "vault:read")
        tools = self.mcp(tokens["access_token"], "tools/list").json()["result"]["tools"]
        self.assertEqual(len(tools), 4)
        result = self.mcp(
            tokens["access_token"], "tools/call",
            {"name": "create_note", "arguments": {"folder": "00_INBOX", "title": "x", "content": "x"}},
        ).json()["result"]
        self.assertTrue(result["isError"])
        self.assertFalse(PendingChange.objects.exists())

    def test_tool_errors_are_results_not_crashes(self):
        _, tokens = self.connect()
        token = tokens["access_token"]
        for name, args in [
            ("read_note", {"path": "../etc/passwd"}),
            ("read_note", {"path": "02_Ideas/none.md"}),
            ("create_note", {"folder": "99_Archive", "title": "x", "content": "x"}),
            ("search_notes", {"query": ""}),
            ("search_notes", {"query": 123}),
            ("read_note", {"path": ["x"]}),
            ("read_note", {"path": "02_Ideas/料金案.md", "offset": "abc"}),
            ("create_note", {"folder": "00_INBOX", "title": 5, "content": "x"}),
            ("update_note", {"path": "02_Ideas/料金案.md", "content": "x", "base_hash": "0" * 64, "base_updated_at": 5}),
        ]:
            with self.subTest(name=name, args=args):
                result = self.mcp(token, "tools/call", {"name": name, "arguments": args}).json()["result"]
                self.assertTrue(result["isError"])
        unknown = self.mcp(token, "tools/call", {"name": "delete_note", "arguments": {}}).json()
        self.assertEqual(unknown["error"]["code"], -32602)

    def test_protocol_errors(self):
        _, tokens = self.connect()
        token = tokens["access_token"]
        self.assertEqual(self.mcp(token, "resources/list").json()["error"]["code"], -32601)
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        self.assertEqual(self.client.get("/api/vault/mcp", **headers).status_code, 405)
        bad = self.client.post("/api/vault/mcp", data="{", content_type="application/json", **headers)
        self.assertEqual(bad.json()["error"]["code"], -32700)
        batch = self.client.post("/api/vault/mcp", data="[]", content_type="application/json", **headers)
        self.assertEqual(batch.json()["error"]["code"], -32600)

    def test_static_api_keys_work_for_cli_clients(self):
        for key, count in [(READ_KEY, 4), (SYNC_KEY, 4), (WRITE_KEY, 7)]:
            with self.subTest(count=count):
                tools = self.mcp(key, "tools/list").json()["result"]["tools"]
                self.assertEqual(len(tools), count)

    def test_token_bound_to_this_server(self):
        _, tokens = self.connect()
        with override_settings(VAULT_PUBLIC_URL="https://other.example.com"):
            self.assertEqual(self.mcp(tokens["access_token"], "ping").status_code, 401)

    def test_expired_access_token(self):
        _, tokens = self.connect()
        OAuthToken.objects.update(access_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.mcp(tokens["access_token"], "ping").status_code, 401)

    def test_revoke_command(self):
        _, tokens = self.connect()
        out = io.StringIO()
        call_command("vault_revoke_tokens", stdout=out)
        self.assertIn("Revoked 1", out.getvalue())
        self.assertEqual(self.mcp(tokens["access_token"], "ping").status_code, 401)


class OwnerPasswordCommandTests(ApiTestCase):
    def test_hash_from_stdin(self):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(PASSWORD + "\n")):
            call_command("vault_owner_password", "--stdin", stdout=out)
        line = out.getvalue().strip()
        self.assertTrue(line.startswith("VAULT_OWNER_PASSWORD_HASH='"))
        self.assertTrue(check_password(PASSWORD, line.split("'")[1]))
        self.assertNotIn(PASSWORD, line)

    def test_short_password_rejected(self):
        with mock.patch("sys.stdin", io.StringIO("short")), self.assertRaises(CommandError):
            call_command("vault_owner_password", "--stdin", stdout=io.StringIO())
