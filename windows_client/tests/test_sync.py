import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import requests

import config as config_module
import sync
from config import Config, ConfigError, load_config, validate_api_url

API_KEY = "k" * 40


class FakeClient:
    """Stands in for VaultApiClient; records pushes and answers like the server."""

    def __init__(self):
        self.pushes = []
        self.fail = False
        self.reject = {}

    def push(self, notes):
        if self.fail:
            raise sync.ApiError("connection refused")
        self.pushes.append(notes)
        results = []
        for note in notes:
            if note["path"] in self.reject:
                results.append({"path": note["path"], "status": "rejected", "error": self.reject[note["path"]]})
            else:
                results.append({"path": note["path"], "status": "created"})
        return results

    def pushed_paths(self):
        return [note["path"] for batch in self.pushes for note in batch]


class VaultTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "WorkVault"
        self.root.mkdir()
        self.config = Config(
            vault_root=self.root.resolve(),
            api_url="https://vault.example.com/api/vault",
            api_key=API_KEY,
            state_file=base / "data" / "state.json",
            log_file=base / "data" / "sync.log",
        )
        self.client = FakeClient()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, content, encoding="utf-8"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode(encoding) if isinstance(content, str) else content
        path.write_bytes(data)
        return path

    def syncer(self, config=None):
        config = config or self.config
        return sync.Syncer(config, self.client, sync.SyncState(config))

    def bump_mtime(self, rel):
        path = self.root / rel
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


class SyncTests(VaultTestCase):
    def populate(self):
        self.write("00_INBOX.md", "inbox")
        self.write("01_Projects/A/project.md", "---\ntags: [a]\n---\n[[link]] ![[img.png]] #tag\r\n")
        self.write("02_Ideas/アイデア.md", "日本語ノート")
        self.write(".obsidian/workspace.json", "{}")
        self.write(".obsidian/notes.md", "config")
        self.write(".trash/old.md", "old")
        self.write("01_Projects/.hidden.md", "hidden")
        self.write("secrets.md", "nope")
        self.write("Password/list.md", "nope")
        self.write("03_Knowledge/_attachments/pic.png", b"\x89PNG")
        self.write(".env", "VAULT_API_KEY=x")
        self.write("04_AI-Chats/leak.md", "OPENAI_API_KEY=sk-proj-" + "a" * 40)

    def test_first_sync_sends_only_eligible_notes_verbatim(self):
        self.populate()
        self.assertTrue(self.syncer().full_scan())
        self.assertEqual(
            sorted(self.client.pushed_paths()),
            ["00_INBOX.md", "01_Projects/A/project.md", "02_Ideas/アイデア.md"],
        )
        sent = {n["path"]: n for batch in self.client.pushes for n in batch}
        project = sent["01_Projects/A/project.md"]
        self.assertEqual(project["content"], "---\ntags: [a]\n---\n[[link]] ![[img.png]] #tag\r\n")
        self.assertEqual(project["content_hash"], hashlib.sha256(project["content"].encode()).hexdigest())
        self.assertIn("modified_at", project)

    def test_unchanged_notes_are_not_resent(self):
        self.populate()
        self.syncer().full_scan()
        self.client.pushes.clear()
        self.assertTrue(self.syncer().full_scan())  # fresh Syncer: state comes from disk
        self.assertEqual(self.client.pushes, [])

    def test_changed_note_is_sent_and_touched_note_is_not(self):
        self.write("a.md", "v1")
        self.write("b.md", "same")
        self.syncer().full_scan()
        self.client.pushes.clear()

        self.write("a.md", "v2")
        self.bump_mtime("a.md")
        self.bump_mtime("b.md")  # metadata only
        syncer = self.syncer()
        syncer.sync_paths(["a.md", "b.md"])
        self.assertEqual(self.client.pushed_paths(), ["a.md"])
        self.assertEqual(self.client.pushes[0][0]["content"], "v2")

        self.client.pushes.clear()
        syncer.sync_paths(["b.md"])
        self.assertEqual(self.client.pushes, [])

    def test_failed_push_is_retried_later(self):
        self.write("a.md", "v1")
        self.client.fail = True
        self.assertFalse(self.syncer().full_scan())
        self.client.fail = False
        self.assertTrue(self.syncer().full_scan())
        self.assertEqual(self.client.pushed_paths(), ["a.md"])

    def test_server_rejection_is_not_resent_until_changed(self):
        self.write("a.md", "v1")
        self.client.reject = {"a.md": "invalid_path"}
        self.syncer().full_scan()
        self.syncer().full_scan()
        self.assertEqual(self.client.pushed_paths(), ["a.md"])
        self.write("a.md", "v2")
        self.bump_mtime("a.md")
        self.syncer().full_scan()
        self.assertEqual(self.client.pushed_paths(), ["a.md", "a.md"])

    def test_secret_scan_skip_and_allow_list(self):
        self.write("leak.md", "-----BEGIN RSA PRIVATE KEY-----")
        with self.assertLogs("vault_sync", "WARNING") as logs:
            self.syncer().full_scan()
        self.assertEqual(self.client.pushes, [])
        self.assertIn("possible_secret:private_key", logs.output[0])
        self.assertNotIn("BEGIN", "".join(logs.output))

        allowed = replace(self.config, secret_scan_allow=("leak.md",))
        self.syncer(allowed).full_scan()  # rules changed -> state reset -> re-evaluated
        self.assertEqual(self.client.pushed_paths(), ["leak.md"])

    def test_non_utf8_and_oversized_files_are_skipped(self):
        self.write("sjis.md", "日本語", encoding="cp932")
        self.write("big.md", "x" * 100)
        small = replace(self.config, max_note_bytes=50)
        self.syncer(small).full_scan()
        self.assertEqual(self.client.pushes, [])
        state = json.loads(self.config.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["notes"]["sjis.md"]["skipped"], "not_utf8")
        self.assertEqual(state["notes"]["big.md"]["skipped"], "too_large")

    def test_deleted_files_are_ignored(self):
        self.syncer().sync_paths(["gone.md"])
        self.assertEqual(self.client.pushes, [])

    def test_batches_respect_count_and_size(self):
        for i in range(5):
            self.write(f"n{i}.md", "x" * 100)
        config = replace(self.config, batch_max_notes=2)
        self.syncer(config).full_scan()
        self.assertEqual([len(b) for b in self.client.pushes], [2, 2, 1])

        self.client.pushes.clear()
        config = replace(self.config, batch_max_bytes=300)
        self.syncer(config).full_scan(force=True)
        self.assertTrue(all(len(b) == 1 for b in self.client.pushes))

    def test_state_resets_for_another_server(self):
        self.write("a.md", "v1")
        self.syncer().full_scan()
        other = replace(self.config, api_url="https://other.example.com/api/vault")
        self.syncer(other).full_scan()
        self.assertEqual(self.client.pushed_paths(), ["a.md", "a.md"])

    def test_state_file_is_outside_vault_and_has_no_content(self):
        self.write("a.md", "本文の秘密")
        self.syncer().full_scan()
        text = self.config.state_file.read_text(encoding="utf-8")
        self.assertNotIn("本文の秘密", text)
        self.assertFalse(str(self.config.state_file).startswith(str(self.root)))

    def test_dry_run_reports_without_sending(self):
        self.populate()
        out = io.StringIO()
        sync.dry_run(self.config, out=out)
        report = out.getvalue()
        self.assertIn(".obsidian/  [obsidian_config]", report)
        self.assertIn("secrets.md  [pattern:secrets*]", report)
        self.assertIn("04_AI-Chats/leak.md  [possible_secret:openai_key]", report)
        self.assertIn("+ 02_Ideas/アイデア.md", report)
        self.assertNotIn("日本語ノート", report)
        self.assertFalse(self.config.state_file.exists())


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []
        self.verify = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ApiClientTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(vault_root=Path("."), api_url="https://vault.example.com/api/vault/", api_key=API_KEY)
        self.sleeps = []

    def client(self, responses):
        session = FakeSession(responses)
        return sync.VaultApiClient(self.config, session=session, sleep=self.sleeps.append), session

    def test_push_sends_utf8_json_with_bearer_key(self):
        client, session = self.client([FakeResponse(200, {"results": [{"status": "created"}]})])
        client.push([{"path": "a.md", "content": "日本語"}])
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("POST", "https://vault.example.com/api/vault/sync/"))
        self.assertEqual(session.headers["Authorization"], f"Bearer {API_KEY}")
        self.assertIn("日本語".encode("utf-8"), kwargs["data"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(session.verify)

    def test_retries_transient_failures(self):
        client, session = self.client(
            [
                requests.ConnectionError("down"),
                FakeResponse(503),
                FakeResponse(429, headers={"Retry-After": "7"}),
                FakeResponse(200, {"results": [{"status": "created"}]}),
            ]
        )
        client.push([{"path": "a.md"}])
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(self.sleeps, [2.0, 4.0, 7.0])

    def test_gives_up_after_attempts(self):
        client, _ = self.client([FakeResponse(503)] * 4)
        with self.assertRaises(sync.ApiError):
            client.push([{"path": "a.md"}])

    def test_client_errors_and_redirects_are_not_retried(self):
        for response in [
            FakeResponse(401, {"error": {"code": "unauthorized", "message": "Missing or invalid API key."}}),
            FakeResponse(301, headers={"Location": "https://elsewhere"}),
        ]:
            client, session = self.client([response])
            with self.subTest(status=response.status_code), self.assertRaises(sync.ApiError) as ctx:
                client.push([{"path": "a.md"}])
            self.assertEqual(len(session.calls), 1)
            self.assertNotIn(API_KEY, str(ctx.exception))


class ConfigTests(unittest.TestCase):
    def test_api_url_must_be_https_except_localhost(self):
        for url in ["https://vault.example.com/api/vault", "http://localhost:8000/api/vault", "http://127.0.0.1:8000"]:
            validate_api_url(url)
        for url in ["http://vault.example.com/api/vault", "ftp://x", "vault.example.com", ""]:
            with self.subTest(url=url), self.assertRaises(ConfigError):
                validate_api_url(url)

    def test_load_config_validates_key_and_hides_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"VAULT_ROOT": tmp, "VAULT_API_URL": "https://vault.example.com/api/vault", "VAULT_API_KEY": "short"}
            with self.assertRaises(ConfigError):
                load_config(environ=env)
            env["VAULT_API_KEY"] = API_KEY
            env["VAULT_EXTRA_EXCLUDES"] = "04_AI-Chats\\Claude_Export_*, 99_Archive/*"
            config = load_config(environ=env)
            self.assertNotIn(API_KEY, repr(config))
            self.assertEqual(config.extra_excludes, ("04_AI-Chats/Claude_Export_*", "99_Archive/*"))
            self.assertEqual(config.debounce_seconds, 2.5)

    def test_dry_run_needs_no_api_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            load_config(environ={"VAULT_ROOT": tmp}, require_api=False)

    def test_missing_vault_root(self):
        with self.assertRaises(ConfigError):
            load_config(environ={"VAULT_ROOT": "Z:/definitely/missing"}, require_api=False)


class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "state.json.lock"
            with sync.SingleInstance(lock):
                with self.assertRaises(ConfigError):
                    with sync.SingleInstance(lock):
                        pass
            with sync.SingleInstance(lock):  # released after exit
                pass


if __name__ == "__main__":
    unittest.main()
