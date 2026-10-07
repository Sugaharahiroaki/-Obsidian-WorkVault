import unicodedata

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from vault import auth
from vault.security import InvalidPath, folder_of, normalize_folder_path, normalize_note_path, title_of


class NotePathTests(SimpleTestCase):
    def test_accepts_vault_relative_notes(self):
        for path in [
            "00_INBOX.md",
            "01_Projects/MiseMiru/meetings/2026-10-07_契約決済_履歴整理.md",
            "04_AI-Chats/Claude_Export_2026-10-07/001_eLTAX と住民税申請.md",
            "02_Ideas/100%達成.md",
            "03_Knowledge/notes.MD",
        ]:
            with self.subTest(path=path):
                self.assertEqual(normalize_note_path(path), path)

    def test_normalizes_windows_separators_and_unicode(self):
        self.assertEqual(normalize_note_path("01_Projects\\A\\project.md"), "01_Projects/A/project.md")
        nfd = unicodedata.normalize("NFD", "02_Ideas/ガイド.md")
        self.assertEqual(normalize_note_path(nfd), unicodedata.normalize("NFC", "02_Ideas/ガイド.md"))

    def test_rejects_traversal_and_absolute_paths(self):
        for path in [
            "../outside.md",
            "01_Projects/../../outside.md",
            "01_Projects/./a.md",
            "/etc/passwd.md",
            "C:/Users/hsuga/x.md",
            "C:\\Users\\hsuga\\x.md",
            "\\\\server\\share\\x.md",
            "//server/share/x.md",
            "a//b.md",
            "%2e%2e/outside.md",
            "%2e%2e%2foutside.md",
            "..%2foutside.md",
            "..%5coutside.md",
            "%252e%252e%252foutside.md",
            "%25252e%25252e%25252foutside.md",
            "\uff0e\uff0e/outside.md",  # full-width dots
            "x.md:secret",  # NTFS alternate data stream
            "a\x00.md",
            "a%00.md",
        ]:
            with self.subTest(path=path), self.assertRaises(InvalidPath):
                normalize_note_path(path)

    def test_rejects_hidden_and_excluded_files(self):
        for path in [
            ".obsidian/app.json",
            ".obsidian/workspace.md",
            "%2eobsidian/workspace.md",
            "01_Projects/.hidden.md",
            ".env",
            ".env.md",
            "secrets.md",
            "Secrets/token.md",
            "01_Projects/password_list.md",
            "PASSWORD.md",
            "certs/server.pem",
            "deploy.key",
        ]:
            with self.subTest(path=path), self.assertRaises(InvalidPath):
                normalize_note_path(path)

    def test_rejects_non_notes_and_odd_names(self):
        for path in ["note.txt", "image.png", ".md", "", "dir/", "CON.md", "a /b.md", "a./b.md", None, 5]:
            with self.subTest(path=path), self.assertRaises(InvalidPath):
                normalize_note_path(path)
        with self.assertRaises(InvalidPath):
            normalize_note_path("a/" + "x" * 300 + ".md")

    def test_folder_paths(self):
        self.assertEqual(normalize_folder_path(None), "")
        self.assertEqual(normalize_folder_path(""), "")
        self.assertEqual(normalize_folder_path("01_Projects/"), "01_Projects")
        self.assertEqual(normalize_folder_path("01_Projects\\MiseMiru"), "01_Projects/MiseMiru")
        for folder in ["..", "../x", ".obsidian", "/abs", "%2e%2e"]:
            with self.subTest(folder=folder), self.assertRaises(InvalidPath):
                normalize_folder_path(folder)

    def test_title_and_folder(self):
        self.assertEqual(title_of("01_Projects/A/project.md"), "project")
        self.assertEqual(folder_of("01_Projects/A/project.md"), "01_Projects/A")
        self.assertEqual(folder_of("00_INBOX.md"), "")


class ApiKeyConfigTests(SimpleTestCase):
    @override_settings(VAULT_SYNC_KEYS="pc:" + "k" * 40, VAULT_READ_KEYS="gpt:" + "g" * 40 + ", codex:" + "c" * 40)
    def test_parses_scoped_keys(self):
        keys = {k.client: k.scopes for k in auth.load_api_keys()}
        self.assertEqual(keys["pc"], {auth.SCOPE_SYNC, auth.SCOPE_READ})
        self.assertEqual(keys["gpt"], {auth.SCOPE_READ})
        self.assertEqual(keys["codex"], {auth.SCOPE_READ})

    def test_rejects_malformed_keys_without_echoing_them(self):
        bare_key = "z" * 40
        for raw in [bare_key, "pc:short", "bad name:" + "k" * 40, "pc:" + "k" * 20 + " " + "k" * 20]:
            with self.subTest(raw=raw), override_settings(VAULT_SYNC_KEYS=raw):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    auth.load_api_keys()
                self.assertNotIn(bare_key, str(ctx.exception))
                self.assertNotIn("k" * 20, str(ctx.exception))

    def test_repr_hides_digest(self):
        key = auth.ApiKey(client="pc", digest=b"secret-digest", scopes=frozenset({"read"}))
        self.assertNotIn("secret", repr(key))
