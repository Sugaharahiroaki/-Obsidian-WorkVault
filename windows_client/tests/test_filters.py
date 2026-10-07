import ctypes
import os
import tempfile
import unittest
from pathlib import Path

import filters


class PathRuleTests(unittest.TestCase):
    def test_allows_normal_notes(self):
        for rel in [
            "00_INBOX.md",
            "01_Projects/MiseMiru/meetings/2026-10-07_契約決済_履歴整理.md",
            "04_AI-Chats/Claude_Export_2026-10-07/001_eLTAX と住民税申請.md",
        ]:
            with self.subTest(rel=rel):
                self.assertIsNone(filters.path_exclusion_reason(rel))

    def test_never_syncs_sensitive_or_hidden_paths(self):
        cases = {
            ".obsidian/workspace.md": "obsidian_config",
            ".obsidian/plugins/x/README.md": "obsidian_config",
            ".trash/old.md": "hidden_name",
            "01_Projects/.draft.md": "hidden_name",
            ".env": "hidden_name",
            "secrets.md": "pattern:secrets*",
            "Secrets/aws.md": "pattern:secrets*",
            "01_Projects/password-list.md": "pattern:password*",
            "server.key": "pattern:*.key",
            "certs/a.pem": "pattern:*.pem",
            "a.crt": "pattern:*.crt",
            "a.pfx": "pattern:*.pfx",
            "image.png": "not_markdown",
            "a/../b.md": "invalid_path",
        }
        for rel, reason in cases.items():
            with self.subTest(rel=rel):
                self.assertEqual(filters.path_exclusion_reason(rel), reason)

    def test_user_excludes(self):
        excludes = ("04_AI-Chats/Claude_Export_*",)
        self.assertEqual(
            filters.path_exclusion_reason("04_AI-Chats/Claude_Export_2026-10-07/088_x.md", excludes),
            "user_exclude:04_AI-Chats/Claude_Export_*",
        )
        self.assertIsNone(filters.path_exclusion_reason("04_AI-Chats/other.md", excludes))


class FilesystemRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_and_plain_files(self):
        (self.root / "a.md").write_text("x", encoding="utf-8")
        self.assertIsNone(filters.fs_exclusion_reason(self.root / "a.md"))
        self.assertEqual(filters.fs_exclusion_reason(self.root / "nope.md"), "missing")

    def test_symlinks_are_not_followed(self):
        target = self.root / "target.md"
        target.write_text("x", encoding="utf-8")
        link = self.root / "link.md"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("creating symlinks is not permitted here")
        self.assertEqual(filters.fs_exclusion_reason(link), "symlink")

    @unittest.skipUnless(os.name == "nt", "Windows file attributes")
    def test_windows_hidden_attribute(self):
        path = self.root / "hidden.md"
        path.write_text("x", encoding="utf-8")
        ctypes.windll.kernel32.SetFileAttributesW(str(path), filters.FILE_ATTRIBUTE_HIDDEN)
        self.assertEqual(filters.fs_exclusion_reason(path), "hidden_attribute")


class SecretScanTests(unittest.TestCase):
    def test_detects_credentials(self):
        cases = {
            "-----BEGIN OPENSSH PRIVATE KEY-----\nabc": "private_key",
            "-----BEGIN PRIVATE KEY-----": "private_key",
            "key = AKIAIOSFODNN7EXAMPLE": "aws_access_key",
            "token ghp_" + "a" * 36: "github_token",
            "ANTHROPIC_API_KEY=sk-ant-api03-" + "x" * 40: "anthropic_key",
            "OPENAI_API_KEY=sk-proj-" + "y" * 40: "openai_key",
            "sk_live_" + "z" * 24: "stripe_secret",
            "xoxb-1234567890-abcdef": "slack_token",
            "AIza" + "B" * 35: "google_api_key",
            "ntn_" + "c" * 46: "notion_token",
            "カード 4242 4242 4242 4242 期限": "card_number",
            "4242-4242-4242-4242": "card_number",
            "3782 822463 10005": "card_number",  # Amex test number
        }
        for content, name in cases.items():
            with self.subTest(name=name):
                self.assertEqual(filters.find_secret(content), name)

    def test_ignores_ordinary_text(self):
        for content in [
            "APIキーは .env に置く。sk-xxxx のような値はVaultに書かない。",
            "電話 090-1234-5678",
            "注文番号 250-1234567-1234567",
            "1234 5678 9012 3456",  # fails Luhn
            "タイムスタンプ 1696234567890123",
            "[[01_Projects/MiseMiru/project]] #tag",
        ]:
            with self.subTest(content=content):
                self.assertIsNone(filters.find_secret(content))


if __name__ == "__main__":
    unittest.main()
