import hashlib
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import applier

NOW = datetime(2026, 10, 7, 13, 20, 0)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ApplierTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "WorkVault"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def read(self, rel):
        return (self.root / rel).read_bytes().decode("utf-8")

    def apply(self, op, rel, content, base_hash="", change_id=1):
        change = {"id": change_id, "op": op, "path": rel, "content": content, "base_hash": base_hash}
        return applier.apply_change(self.root, change, now=NOW)

    def all_files(self):
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())


class CreateTests(ApplierTestCase):
    def test_creates_file_and_folders_byte_for_byte(self):
        content = "---\nsource: chatgpt\n---\n# 料金案\r\n[[TOMONI]] #tag\n"
        result = self.apply("create", "00_INBOX/2026/メモ.md", content)
        self.assertEqual(result, {"id": 1, "status": "applied", "detail": ""})
        self.assertEqual((self.root / "00_INBOX/2026/メモ.md").read_bytes(), content.encode("utf-8"))

    def test_never_overwrites_existing_file(self):
        self.write("02_Ideas/a.md", "PCの内容")
        result = self.apply("create", "02_Ideas/a.md", "AIの内容")
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["conflict_path"], "02_Ideas/a (conflict 2026-10-07 132000).md")
        self.assertEqual(result["detail"], "exists_on_pc")
        self.assertEqual(self.read("02_Ideas/a.md"), "PCの内容")
        self.assertEqual(self.read(result["conflict_path"]), "AIの内容")

    def test_same_content_counts_as_applied(self):
        self.write("02_Ideas/a.md", "same")
        self.assertEqual(self.apply("create", "02_Ideas/a.md", "same")["detail"], "already_present")

    def test_conflict_names_are_unique(self):
        self.write("02_Ideas/a.md", "x")
        first = self.apply("create", "02_Ideas/a.md", "1")
        second = self.apply("create", "02_Ideas/a.md", "2", change_id=2)
        self.assertNotEqual(first["conflict_path"], second["conflict_path"])
        self.assertEqual(second["conflict_path"], "02_Ideas/a (conflict 2026-10-07 132000 2).md")


class UpdateAppendTests(ApplierTestCase):
    def test_update_when_unchanged_on_pc(self):
        self.write("01_Projects/A/project.md", "v1")
        result = self.apply("update", "01_Projects/A/project.md", "v2", base_hash=sha("v1"))
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.read("01_Projects/A/project.md"), "v2")
        self.assertEqual(self.all_files(), ["01_Projects/A/project.md"])  # no temp files left

    def test_update_conflict_keeps_pc_version(self):
        self.write("01_Projects/A/project.md", "PCで編集済み")
        result = self.apply("update", "01_Projects/A/project.md", "AI版", base_hash=sha("v1"))
        self.assertEqual((result["status"], result["detail"]), ("conflict", "changed_on_pc"))
        self.assertEqual(self.read("01_Projects/A/project.md"), "PCで編集済み")
        self.assertEqual(self.read(result["conflict_path"]), "AI版")

    def test_update_already_applied(self):
        self.write("02_Ideas/a.md", "v2")
        result = self.apply("update", "02_Ideas/a.md", "v2", base_hash=sha("v1"))
        self.assertEqual((result["status"], result["detail"]), ("applied", "already_present"))

    def test_append_keeps_pc_edits_and_newline_style(self):
        self.write("02_Ideas/a.md", "line1\r\nPCで追加")
        result = self.apply("append", "02_Ideas/a.md", "AI追記\n2行目", base_hash=sha("line1\r\n"))
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.read("02_Ideas/a.md"), "line1\r\nPCで追加\r\nAI追記\r\n2行目\r\n")

    def test_missing_file_becomes_conflict(self):
        for op in ("update", "append"):
            with self.subTest(op=op):
                result = self.apply(op, "02_Ideas/gone.md", f"{op} text", base_hash=sha("x"))
                self.assertEqual((result["status"], result["detail"]), ("conflict", "missing_on_pc"))
                self.assertFalse((self.root / "02_Ideas/gone.md").exists())
                (self.root / result["conflict_path"]).unlink()

    def test_append_text_matches_server_rule(self):
        self.assertEqual(applier.append_text("a\n", "b"), "a\nb\n")
        self.assertEqual(applier.append_text("a", "b\n"), "a\nb\n")
        self.assertEqual(applier.append_text("a\r\nb\r\n", "c\nd"), "a\r\nb\r\nc\r\nd\r\n")
        self.assertEqual(applier.append_text("", "x"), "x\n")


class RefusalTests(ApplierTestCase):
    def test_refuses_paths_outside_writable_folders(self):
        self.write("99_Archive/old.md", "old")
        self.write("00_INBOX.md", "inbox original")
        cases = {
            "99_Archive/old.md": "not_writable",
            "08_Templates/x.md": "not_writable",
            "00_INBOX.md": "not_writable",
            "AGENTS.md": "not_writable",
            ".obsidian/x.md": "obsidian_config",
            "02_Ideas/.hidden.md": "hidden_name",
            "02_Ideas/secrets.md": "pattern:secrets*",
            "02_Ideas/../../x.md": "invalid_path",
            "02_Ideas\\x.md": "invalid_path",
            "C:/x.md": "invalid_path",
            "02_Ideas/x.txt": "not_markdown",
        }
        for rel, reason in cases.items():
            for op in ("create", "update", "append"):
                with self.subTest(rel=rel, op=op):
                    result = self.apply(op, rel, "AI", base_hash=sha("old"))
                    self.assertEqual((result["status"], result["detail"]), ("failed", reason))
        self.assertEqual(self.read("99_Archive/old.md"), "old")
        self.assertEqual(self.read("00_INBOX.md"), "inbox original")
        self.assertEqual(self.all_files(), ["00_INBOX.md", "99_Archive/old.md"])

    def test_refuses_invalid_change(self):
        self.assertEqual(self.apply("delete", "02_Ideas/a.md", "x")["detail"], "invalid_change")
        self.assertEqual(applier.apply_change(self.root, {"id": 1, "op": "create", "path": "02_Ideas/a.md"})["status"], "failed")

    def test_refuses_writing_through_symlinked_folder(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.root / "02_Ideas").mkdir()
        try:
            os.symlink(outside, self.root / "02_Ideas" / "link", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("creating symlinks is not permitted here")
        result = self.apply("create", "02_Ideas/link/x.md", "AI")
        self.assertEqual((result["status"], result["detail"]), ("failed", "symlink"))
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
