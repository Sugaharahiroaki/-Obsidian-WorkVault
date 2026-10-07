import json
from datetime import datetime

from django.test import SimpleTestCase, override_settings

from vault.models import AccessLog, PendingChange, VaultNote
from vault.security import InvalidPath, conflict_path_for, ensure_writable, note_path_from_title
from vault.services.writes import append_text, has_frontmatter

from .test_api import READ_KEY, SYNC_KEY, ApiTestCase, sha256

WRITE_KEY = "w" * 40
KEYS = {"VAULT_WRITE_KEYS": "test-gpt:" + WRITE_KEY}

FRONTMATTER_NOTE = "---\ntags: [project]\n---\n# 本文\n[[link]] #tag\n"


class WritePathRuleTests(SimpleTestCase):
    def test_title_becomes_safe_file_name(self):
        self.assertEqual(
            note_path_from_title("04_AI-Chats", "2026-10-07 会議: 要約?"), "04_AI-Chats/2026-10-07 会議_ 要約_.md"
        )
        self.assertEqual(note_path_from_title("02_Ideas", "  [[x]] #y | z  "), "02_Ideas/__x__ _y _ z.md")
        self.assertEqual(note_path_from_title("02_Ideas", "already.md"), "02_Ideas/already.md")
        for title in ["", "   ", "...", None]:
            with self.subTest(title=title), self.assertRaises(InvalidPath):
                note_path_from_title("02_Ideas", title)
        with self.assertRaises(InvalidPath):
            note_path_from_title("../x", "t")

    def test_only_listed_folders_are_writable(self):
        for path in ["00_INBOX/memo.md", "01_Projects/A/project.md", "07_DevLogs/log.md"]:
            self.assertEqual(ensure_writable(path), path)
        for path in ["00_INBOX.md", "AGENTS.md", "99_Archive/old.md", "08_Templates/x.md", "01_projects/x.md"]:
            with self.subTest(path=path), self.assertRaises(InvalidPath):
                ensure_writable(path)

    def test_conflict_names(self):
        when = datetime(2026, 10, 7, 13, 20, 0)
        self.assertEqual(conflict_path_for("02_Ideas/a.md", when), "02_Ideas/a (conflict 2026-10-07 132000).md")
        self.assertEqual(conflict_path_for("a.md", when, 2), "a (conflict 2026-10-07 132000 2).md")

    def test_append_text(self):
        self.assertEqual(append_text("a\n", "b"), "a\nb\n")
        self.assertEqual(append_text("a", "b\n"), "a\nb\n")
        self.assertEqual(append_text("a\r\nb\r\n", "c\nd"), "a\r\nb\r\nc\r\nd\r\n")
        self.assertEqual(append_text("", "x"), "x\n")

    def test_frontmatter_detection(self):
        self.assertTrue(has_frontmatter(FRONTMATTER_NOTE))
        self.assertTrue(has_frontmatter("﻿---\r\na: 1\r\n---\r\nbody"))
        self.assertFalse(has_frontmatter("# no frontmatter\n---\n"))


@override_settings(**KEYS)
class WriteApiTests(ApiTestCase):
    def post(self, url, body, key=WRITE_KEY):
        return self.client.post(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {key}",
        )

    def setUp(self):
        self.sync([self.note("01_Projects/A/project.md", FRONTMATTER_NOTE), self.note("02_Ideas/crlf.md", "a\r\nb\r\n")])

    def test_write_scope_is_required(self):
        body = {"folder": "02_Ideas", "title": "x", "content": "x"}
        self.assertEqual(self.post("/api/vault/note/create/", body, key=READ_KEY).status_code, 403)
        self.assertEqual(self.post("/api/vault/note/create/", body, key=SYNC_KEY).status_code, 403)
        self.assertEqual(self.post("/api/vault/note/create/", body).status_code, 201)

    def test_create_note(self):
        response = self.post(
            "/api/vault/note/create/",
            {"folder": "04_AI-Chats", "title": "2026-10-07 ChatGPT: 料金案", "content": "---\nsource: chatgpt\n---\n本文"},
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["path"], "04_AI-Chats/2026-10-07 ChatGPT_ 料金案.md")
        self.assertFalse(body["applied_to_pc"])

        change = PendingChange.objects.get()
        self.assertEqual((change.op, change.path, change.client), ("create", body["path"], "test-gpt"))
        note = self.get("/api/vault/note/", path=body["path"]).json()
        self.assertEqual(note["content"], "---\nsource: chatgpt\n---\n本文")
        self.assertEqual(self.get("/api/vault/search/", q="料金案").json()["count"], 1)

    def test_create_into_inbox_folder_and_refusals(self):
        ok = self.post("/api/vault/note/create/", {"folder": "00_INBOX", "title": "メモ", "content": "x"})
        self.assertEqual(ok.status_code, 201)
        for body in [
            {"folder": "99_Archive", "title": "x", "content": "x"},
            {"folder": "08_Templates", "title": "x", "content": "x"},
            {"folder": "", "title": "AGENTS", "content": "x"},
            {"folder": ".obsidian", "title": "x", "content": "x"},
            {"folder": "../secret", "title": "x", "content": "x"},
            {"path": "00_INBOX.md", "content": "x"},
            {"folder": "02_Ideas", "title": "secrets", "content": "x"},
        ]:
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/vault/note/create/", body).status_code, 400)
        self.assertEqual(self.post("/api/vault/note/create/", {"folder": "02_Ideas", "title": "x", "content": " "}).status_code, 400)

    def test_create_never_overwrites(self):
        response = self.post("/api/vault/note/create/", {"path": "01_Projects/A/project.md", "content": "x"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(VaultNote.objects.get(path="01_Projects/A/project.md").content, FRONTMATTER_NOTE)
        self.post("/api/vault/note/create/", {"folder": "02_Ideas", "title": "new", "content": "1"})
        self.assertEqual(self.post("/api/vault/note/create/", {"folder": "02_Ideas", "title": "new", "content": "2"}).status_code, 409)

    def test_append_keeps_body_and_newline_style(self):
        response = self.post("/api/vault/note/append/", {"path": "02_Ideas/crlf.md", "content": "c\nd"})
        self.assertEqual(response.status_code, 200)
        note = VaultNote.objects.get(path="02_Ideas/crlf.md")
        self.assertEqual(note.content, "a\r\nb\r\nc\r\nd\r\n")
        self.assertEqual(response.json()["content_hash"], sha256(note.content))
        change = PendingChange.objects.get()
        self.assertEqual((change.op, change.content, change.base_hash), ("append", "c\nd", sha256("a\r\nb\r\n")))

    def test_append_requires_existing_writable_note(self):
        self.assertEqual(self.post("/api/vault/note/append/", {"path": "02_Ideas/none.md", "content": "x"}).status_code, 404)
        self.sync([self.note("99_Archive/old.md", "old")])
        self.assertEqual(self.post("/api/vault/note/append/", {"path": "99_Archive/old.md", "content": "x"}).status_code, 400)

    def test_update_with_current_hash(self):
        current = self.get("/api/vault/note/", path="01_Projects/A/project.md").json()
        new = FRONTMATTER_NOTE + "追記\n"
        response = self.post(
            "/api/vault/note/update/",
            {"path": current["path"], "content": new, "base_hash": current["content_hash"], "base_updated_at": current["updated_at"]},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(VaultNote.objects.get(path=current["path"]).content, new)
        change = PendingChange.objects.get()
        self.assertEqual((change.op, change.base_hash), ("update", current["content_hash"]))

    def test_stale_update_is_saved_as_conflict(self):
        stale_hash = sha256(FRONTMATTER_NOTE)
        self.sync([self.note("01_Projects/A/project.md", FRONTMATTER_NOTE + "PCで編集\n")])
        response = self.post(
            "/api/vault/note/update/",
            {"path": "01_Projects/A/project.md", "content": FRONTMATTER_NOTE + "AIの案\n", "base_hash": stale_hash},
        )
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["status"], "conflict")
        self.assertEqual(body["original_path"], "01_Projects/A/project.md")
        self.assertRegex(body["path"], r"^01_Projects/A/project \(conflict \d{4}-\d\d-\d\d \d{6}\)\.md$")
        self.assertEqual(VaultNote.objects.get(path="01_Projects/A/project.md").content, FRONTMATTER_NOTE + "PCで編集\n")
        self.assertEqual(VaultNote.objects.get(path=body["path"]).content, FRONTMATTER_NOTE + "AIの案\n")
        change = PendingChange.objects.get()
        self.assertEqual((change.op, change.path), ("create", body["path"]))

    def test_stale_updated_at_is_a_conflict(self):
        current = self.get("/api/vault/note/", path="02_Ideas/crlf.md").json()
        response = self.post(
            "/api/vault/note/update/",
            {"path": current["path"], "content": "x", "base_hash": current["content_hash"], "base_updated_at": "2020-01-01T00:00:00+09:00"},
        )
        self.assertEqual(response.status_code, 409)

    def test_update_guards(self):
        good_hash = sha256(FRONTMATTER_NOTE)
        cases = [
            ({"path": "01_Projects/A/project.md", "content": "# frontmatter gone", "base_hash": good_hash}, 422),
            ({"path": "01_Projects/A/project.md", "content": FRONTMATTER_NOTE}, 400),
            ({"path": "01_Projects/A/project.md", "content": FRONTMATTER_NOTE, "base_hash": "abc"}, 400),
            ({"path": "02_Ideas/none.md", "content": "x", "base_hash": good_hash}, 404),
        ]
        for body, status in cases:
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/vault/note/update/", body).status_code, status)
        self.assertFalse(PendingChange.objects.exists())

    @override_settings(VAULT_MAX_NOTE_BYTES=20, **KEYS)
    def test_size_limit(self):
        response = self.post("/api/vault/note/append/", {"path": "02_Ideas/crlf.md", "content": "x" * 30})
        self.assertEqual(response.status_code, 413)

    def test_write_logs_have_no_body(self):
        self.post("/api/vault/note/create/", {"folder": "02_Ideas", "title": "log", "content": "秘密の本文"})
        log = AccessLog.objects.filter(action="create").get()
        self.assertEqual((log.client, log.path, log.success), ("test-gpt", "02_Ideas/log.md", True))
        self.assertNotIn("秘密", log.detail)


@override_settings(**KEYS)
class ChangeQueueTests(ApiTestCase):
    def setUp(self):
        self.sync([self.note("02_Ideas/a.md", "a\n")])
        for i in range(2):
            self.client.post(
                "/api/vault/note/append/",
                data=json.dumps({"path": "02_Ideas/a.md", "content": f"line{i}"}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {WRITE_KEY}",
            )

    def ack(self, results, key=SYNC_KEY):
        return self.client.post(
            "/api/vault/changes/ack/",
            data=json.dumps({"results": results}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {key}",
        )

    def test_only_sync_key_can_pull(self):
        self.assertEqual(self.get("/api/vault/changes/", key=WRITE_KEY).status_code, 403)
        self.assertEqual(self.get("/api/vault/changes/", key=READ_KEY).status_code, 403)
        body = self.get("/api/vault/changes/", key=SYNC_KEY).json()
        self.assertEqual([c["content"] for c in body["changes"]], ["line0", "line1"])
        self.assertEqual(body["changes"][0]["base_hash"], sha256("a\n"))

    def test_acknowledge(self):
        first, second = PendingChange.objects.order_by("id")
        response = self.ack(
            [
                {"id": first.pk, "status": "applied"},
                {"id": second.pk, "status": "conflict", "conflict_path": "02_Ideas/a (conflict 2026-10-07 132000).md"},
                {"id": 999, "status": "applied"},
                {"id": first.pk, "status": "bogus"},
            ]
        )
        self.assertEqual(
            [(r["id"], r["ok"], r.get("error")) for r in response.json()["results"]],
            [(first.pk, True, None), (second.pk, True, None), (999, False, "not_found"), (first.pk, False, "invalid_status")],
        )
        second.refresh_from_db()
        self.assertEqual(second.status, "conflict")
        self.assertEqual(second.conflict_path, "02_Ideas/a (conflict 2026-10-07 132000).md")
        self.assertEqual(self.get("/api/vault/changes/", key=SYNC_KEY).json()["count"], 0)
        # Re-sending the same acknowledgement is fine; changing it is not.
        again = self.ack([{"id": first.pk, "status": "applied"}, {"id": first.pk, "status": "failed"}]).json()["results"]
        self.assertEqual([r["ok"] for r in again], [True, False])

    def test_conflict_path_must_be_valid(self):
        change = PendingChange.objects.first()
        result = self.ack([{"id": change.pk, "status": "conflict", "conflict_path": "../x.md"}]).json()["results"][0]
        self.assertEqual(result["error"], "invalid_conflict_path")


@override_settings(**KEYS)
class ReadExtrasTests(ApiTestCase):
    def setUp(self):
        self.sync([self.note("02_Ideas/long.md", "あ" * 25), self.note("01_Projects/A/x.md", "x")])

    def test_paged_read(self):
        page = self.get("/api/vault/note/", path="02_Ideas/long.md", max_chars="10").json()
        self.assertEqual((len(page["content"]), page["total_chars"], page["truncated"], page["next_offset"]), (10, 25, True, 10))
        self.assertEqual(page["content_hash"], sha256("あ" * 25))
        last = self.get("/api/vault/note/", path="02_Ideas/long.md", offset="20", max_chars="10").json()
        self.assertEqual((len(last["content"]), last["truncated"], last["next_offset"]), (5, False, None))
        whole = self.get("/api/vault/note/", path="02_Ideas/long.md").json()
        self.assertEqual((len(whole["content"]), whole["truncated"]), (25, False))
        self.assertEqual(self.get("/api/vault/note/", path="02_Ideas/long.md", max_chars="0").status_code, 400)

    def test_list(self):
        body = self.get("/api/vault/list/", folder="02_Ideas").json()
        self.assertEqual([n["path"] for n in body["results"]], ["02_Ideas/long.md"])
        self.assertEqual(self.get("/api/vault/list/").json()["count"], 2)
        self.assertEqual(self.get("/api/vault/list/", folder="..").status_code, 400)

    @override_settings(VAULT_PUBLIC_URL="https://vault.example.com", **KEYS)
    def test_openapi_schema_is_public_and_ai_only(self):
        response = self.client.get("/api/vault/openapi.json")
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        self.assertEqual(schema["servers"], [{"url": "https://vault.example.com/api/vault"}])
        operations = {op["operationId"] for item in schema["paths"].values() for op in item.values()}
        self.assertEqual(
            operations,
            {"searchNotes", "readNote", "listNotes", "getRecentNotes", "createNote", "appendNote", "updateNote"},
        )
        self.assertNotIn("/sync/", schema["paths"])
        self.assertNotIn("/changes/", schema["paths"])
        self.assertFalse(schema["paths"]["/search/"]["get"]["x-openai-isConsequential"])
        self.assertNotIn("x-openai-isConsequential", schema["paths"]["/note/update/"]["post"])
