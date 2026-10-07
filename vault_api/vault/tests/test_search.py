import hashlib
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from vault import search
from vault.models import VaultNote
from vault.services import notes


def make_note(path, content, title=None):
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    now = timezone.now()
    return VaultNote.objects.create(
        path=path,
        title=title or path.rsplit("/", 1)[-1][:-3],
        folder=folder,
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        created_at=now,
        updated_at=now,
        last_synced_at=now,
    )


class SearchTests(TestCase):
    def setUp(self):
        make_note("01_Projects/MiseMiru/requirements.md", "契約決済の仕様をまとめる。Stripeで月額課金。")
        make_note("03_Knowledge/Business/履歴整理_全体マップ.md", "Claudeの会話履歴を整理した全体マップ。")
        make_note("04_AI-Chats/2026-10-07_ClaudeCode_履歴確認.md", "Claude Code の履歴確認メモ。#AI")
        make_note("02_Ideas/税金.md", "住民税と所得税のメモ")

    def test_fts_is_installed(self):
        self.assertTrue(search.fts_available())

    def test_japanese_substring_uses_fts(self):
        result = notes.search_notes("履歴整理")
        self.assertEqual(result["mode"], "fts")
        self.assertEqual([r["path"] for r in result["results"]], ["03_Knowledge/Business/履歴整理_全体マップ.md"])

    def test_short_japanese_terms_fall_back_to_like(self):
        result = notes.search_notes("契約")
        self.assertEqual(result["mode"], "like")
        self.assertEqual([r["path"] for r in result["results"]], ["01_Projects/MiseMiru/requirements.md"])
        result = notes.search_notes("税")
        self.assertEqual(result["mode"], "like")
        self.assertEqual([r["path"] for r in result["results"]], ["02_Ideas/税金.md"])

    def test_case_insensitive_ascii(self):
        result = notes.search_notes("claude")
        self.assertEqual(result["mode"], "fts")
        self.assertEqual(result["count"], 2)

    def test_terms_are_anded_and_fullwidth_space_splits(self):
        result = notes.search_notes("Claude　履歴確認")
        self.assertEqual([r["path"] for r in result["results"]], ["04_AI-Chats/2026-10-07_ClaudeCode_履歴確認.md"])

    def test_title_matches_rank_first(self):
        make_note("05_Prompts/other.md", "マップについての雑記 マップ マップ マップ")
        result = notes.search_notes("マップ")
        self.assertEqual(result["results"][0]["path"], "03_Knowledge/Business/履歴整理_全体マップ.md")

    def test_folder_filter(self):
        result = notes.search_notes("Claude", folder="04_AI-Chats")
        self.assertEqual([r["path"] for r in result["results"]], ["04_AI-Chats/2026-10-07_ClaudeCode_履歴確認.md"])
        result = notes.search_notes("契約", folder="01_Projects")
        self.assertEqual(result["count"], 1)

    def test_fts_syntax_cannot_be_injected(self):
        for query in ['"Claude" OR x', "title:Claude", "NEAR(Claude 履歴)", "Clau*", "\"\"\"", "-Claude", "a'b;--"]:
            with self.subTest(query=query):
                notes.search_notes(query)  # must not raise

    def test_falls_back_to_like_when_fts_finds_nothing(self):
        with mock.patch.object(search, "_fts_search", return_value=[]):
            result = notes.search_notes("Stripe")
        self.assertEqual(result["mode"], "like")
        self.assertEqual(result["count"], 1)

    def test_index_follows_updates(self):
        note = VaultNote.objects.get(path="02_Ideas/税金.md")
        note.content = "固定資産税の話に変更"
        note.save()
        self.assertEqual(notes.search_notes("住民税")["count"], 0)
        self.assertEqual(notes.search_notes("固定資産税")["count"], 1)

    def test_snippet_surrounds_match(self):
        result = notes.search_notes("月額課金")
        self.assertIn("月額課金", result["results"][0]["snippet"])

    def test_snippet_skips_frontmatter_when_no_body_match(self):
        snippet = search.make_snippet("---\ntags: [a]\n---\n本文です", ["nomatch"])
        self.assertEqual(snippet, "本文です")

    def test_invalid_queries(self):
        for query in ["", "   ", "x" * 201]:
            with self.subTest(query=query), self.assertRaises(notes.InvalidQuery):
                notes.search_notes(query)

    def test_limit_is_clamped(self):
        self.assertEqual(notes.clamp_limit("0"), 1)
        self.assertEqual(notes.clamp_limit("1000"), notes.MAX_LIMIT)
