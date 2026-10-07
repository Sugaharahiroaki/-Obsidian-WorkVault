import hashlib
import json

from django.test import TestCase, override_settings

from vault.models import AccessLog, VaultNote

SYNC_KEY = "s" * 40
READ_KEY = "r" * 40

OBSIDIAN_NOTE = (
    "\ufeff---\r\n"
    "title: テスト\r\n"
    "tags: [project, ai]\r\n"
    "---\r\n"
    "# 見出し\r\n"
    "リンク [[01_Projects/MiseMiru/project|ミセミル]] と埋め込み ![[diagram.png]]\r\n"
    "タグ #tag/sub と #日本語タグ\r\n"
)


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ApiTestCase(TestCase):
    def get(self, url, key=READ_KEY, **params):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {key}"} if key else {}
        return self.client.get(url, params, **headers)

    def sync(self, notes, key=SYNC_KEY):
        return self.client.post(
            "/api/vault/sync/",
            data=json.dumps({"notes": notes}, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {key}",
        )

    def note(self, path, content, modified_at="2026-10-07T10:00:00+09:00"):
        return {
            "path": path,
            "content": content,
            "content_hash": sha256(content),
            "created_at": "2026-10-01T09:00:00+09:00",
            "modified_at": modified_at,
        }


class AuthTests(ApiTestCase):
    def test_missing_and_wrong_keys_are_rejected_and_logged(self):
        for key in [None, "wrong" * 10]:
            with self.subTest(key=key):
                response = self.get("/api/vault/recent/", key=key)
                self.assertEqual(response.status_code, 401)
                self.assertIn("Bearer", response["WWW-Authenticate"])
        logs = AccessLog.objects.filter(action="recent", success=False)
        self.assertEqual(logs.count(), 2)
        self.assertTrue(all(log.detail == "unauthorized" for log in logs))

    def test_basic_auth_scheme_is_rejected(self):
        response = self.client.get("/api/vault/recent/", HTTP_AUTHORIZATION=f"Basic {READ_KEY}")
        self.assertEqual(response.status_code, 401)

    def test_read_key_cannot_sync(self):
        response = self.sync([self.note("a.md", "x")], key=READ_KEY)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(VaultNote.objects.exists())
        self.assertEqual(AccessLog.objects.get().client, "test-reader")

    def test_sync_key_can_read(self):
        self.assertEqual(self.get("/api/vault/tree/", key=SYNC_KEY).status_code, 200)

    def test_wrong_method(self):
        response = self.get("/api/vault/sync/", key=SYNC_KEY)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")

    @override_settings(VAULT_REQUIRE_HTTPS=True)
    def test_https_is_required(self):
        response = self.get("/api/vault/recent/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "https_required")
        secure = self.client.get("/api/vault/recent/", secure=True, HTTP_AUTHORIZATION=f"Bearer {READ_KEY}")
        self.assertEqual(secure.status_code, 200)

    def test_unknown_url_returns_json_404(self):
        response = self.get("/api/vault/delete/")
        self.assertEqual(response.status_code, 404)


class SyncTests(ApiTestCase):
    def test_create_unchanged_update(self):
        note = self.note("01_Projects/A/project.md", "v1")
        response = self.sync([note])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [{"path": "01_Projects/A/project.md", "status": "created"}])

        stored = VaultNote.objects.get()
        self.assertEqual(stored.title, "project")
        self.assertEqual(stored.folder, "01_Projects/A")
        self.assertEqual(stored.content_hash, sha256("v1"))
        self.assertEqual(stored.updated_at.isoformat(), "2026-10-07T01:00:00+00:00")
        first_synced = stored.last_synced_at

        self.assertEqual(self.sync([note]).json()["summary"]["unchanged"], 1)
        stored.refresh_from_db()
        self.assertGreaterEqual(stored.last_synced_at, first_synced)

        updated = self.note("01_Projects/A/project.md", "v2", modified_at="2026-10-07T11:00:00+09:00")
        self.assertEqual(self.sync([updated]).json()["summary"]["updated"], 1)
        stored.refresh_from_db()
        self.assertEqual(stored.content, "v2")
        self.assertEqual(stored.updated_at.isoformat(), "2026-10-07T02:00:00+00:00")

    def test_obsidian_markdown_is_stored_verbatim(self):
        self.sync([self.note("02_Ideas/note.md", OBSIDIAN_NOTE)])
        stored = VaultNote.objects.get()
        self.assertEqual(stored.content, OBSIDIAN_NOTE)
        self.assertEqual(stored.content_hash, sha256(OBSIDIAN_NOTE))
        body = self.get("/api/vault/note/", path="02_Ideas/note.md").json()
        self.assertEqual(body["content"], OBSIDIAN_NOTE)

    def test_bad_items_are_rejected_individually(self):
        good = self.note("02_Ideas/good.md", "ok")
        bad_hash = {**self.note("02_Ideas/bad.md", "x"), "content_hash": "0" * 64}
        traversal = self.note("../outside.md", "x")
        hidden = self.note(".obsidian/workspace.md", "x")
        secret = self.note("secrets.md", "x")
        no_content = {"path": "02_Ideas/none.md"}
        response = self.sync([good, bad_hash, traversal, hidden, secret, no_content, "junk"])
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(
            [(r["status"], r.get("error")) for r in results],
            [
                ("created", None),
                ("rejected", "hash_mismatch"),
                ("rejected", "invalid_path"),
                ("rejected", "invalid_path"),
                ("rejected", "invalid_path"),
                ("rejected", "invalid_content"),
                ("rejected", "invalid_item"),
            ],
        )
        self.assertEqual(list(VaultNote.objects.values_list("path", flat=True)), ["02_Ideas/good.md"])

    @override_settings(VAULT_MAX_NOTE_BYTES=10)
    def test_note_size_limit(self):
        result = self.sync([self.note("a.md", "あ" * 4)]).json()["results"][0]
        self.assertEqual(result["error"], "too_large")

    @override_settings(VAULT_MAX_BATCH_NOTES=2)
    def test_batch_limit(self):
        response = self.sync([self.note(f"n{i}.md", "x") for i in range(3)])
        self.assertEqual(response.status_code, 413)

    def test_invalid_json_and_payload(self):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {SYNC_KEY}"}
        response = self.client.post("/api/vault/sync/", data="{nope", content_type="application/json", **headers)
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/vault/sync/", data="[]", content_type="application/json", **headers)
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/vault/sync/", data="x", content_type="text/plain", **headers)
        self.assertEqual(response.status_code, 415)

    def test_future_timestamps_are_clamped(self):
        self.sync([self.note("a.md", "x", modified_at="2999-01-01T00:00:00+00:00")])
        self.assertLess(VaultNote.objects.get().updated_at.year, 2999)


class ReadTests(ApiTestCase):
    def setUp(self):
        self.sync(
            [
                self.note("00_INBOX.md", "inbox", modified_at="2026-10-07T08:00:00+09:00"),
                self.note("01_Projects/MiseMiru/project.md", "ミセミル", modified_at="2026-10-07T12:00:00+09:00"),
                self.note("01_Projects/MiseMiru/meetings/m1.md", "会議", modified_at="2026-10-06T12:00:00+09:00"),
                self.note("02_Ideas/idea.md", "アイデア", modified_at="2026-10-07T09:00:00+09:00"),
            ]
        )

    def test_read_note(self):
        body = self.get("/api/vault/note/", path="01_Projects/MiseMiru/project.md").json()
        self.assertEqual(body["title"], "project")
        self.assertEqual(body["folder"], "01_Projects/MiseMiru")
        self.assertEqual(body["content"], "ミセミル")
        self.assertEqual(body["updated_at"], "2026-10-07T12:00:00+09:00")

    def test_read_note_errors(self):
        self.assertEqual(self.get("/api/vault/note/", path="missing.md").status_code, 404)
        for path in ["../x.md", "%2e%2e/x.md", "/etc/passwd", "C:\\x.md", ".obsidian/app.json", ""]:
            with self.subTest(path=path):
                self.assertEqual(self.get("/api/vault/note/", path=path).status_code, 400)

    def test_tree(self):
        tree = self.get("/api/vault/tree/").json()
        self.assertEqual(tree["path"], "")
        names = [(c["type"], c["name"]) for c in tree["children"]]
        self.assertEqual(names, [("folder", "01_Projects"), ("folder", "02_Ideas"), ("note", "00_INBOX.md")])
        misemiru = tree["children"][0]["children"][0]
        self.assertEqual(misemiru["path"], "01_Projects/MiseMiru")
        self.assertEqual([c["name"] for c in misemiru["children"]], ["meetings", "project.md"])

        sub = self.get("/api/vault/tree/", folder="01_Projects/MiseMiru").json()
        self.assertEqual(sub["path"], "01_Projects/MiseMiru")
        self.assertEqual(len(sub["children"]), 2)
        self.assertEqual(self.get("/api/vault/tree/", folder="../").status_code, 400)

    def test_recent(self):
        body = self.get("/api/vault/recent/", limit="2").json()
        self.assertEqual(
            [n["path"] for n in body["results"]], ["01_Projects/MiseMiru/project.md", "02_Ideas/idea.md"]
        )
        body = self.get("/api/vault/recent/", folder="01_Projects").json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(self.get("/api/vault/recent/", limit="abc").status_code, 400)


class AccessLogTests(ApiTestCase):
    def test_logs_metadata_only(self):
        secret_body = "本文に含まれる秘密の内容"
        self.sync([self.note("02_Ideas/private.md", secret_body)])
        self.get("/api/vault/note/", path="02_Ideas/private.md")
        self.get("/api/vault/search/", q="秘密の内容")

        logs = list(AccessLog.objects.order_by("id"))
        self.assertEqual([(log.action, log.success) for log in logs], [("sync", True), ("read", True), ("search", True)])
        self.assertEqual(logs[0].client, "test-pc")
        self.assertEqual(logs[0].path, "02_Ideas/private.md")
        self.assertEqual(logs[1].client, "test-reader")
        self.assertEqual(logs[1].ip, "127.0.0.1")
        for log in logs:
            for value in (log.path, log.detail, log.client):
                self.assertNotIn("秘密", value)
                self.assertNotIn(SYNC_KEY, value)
                self.assertNotIn(READ_KEY, value)

    @override_settings(VAULT_TRUSTED_PROXY_COUNT=1)
    def test_client_ip_behind_trusted_proxy(self):
        self.client.get(
            "/api/vault/recent/",
            HTTP_AUTHORIZATION=f"Bearer {READ_KEY}",
            HTTP_X_FORWARDED_FOR="10.9.9.9, 203.0.113.7",
        )
        self.assertEqual(AccessLog.objects.get().ip, "203.0.113.7")

    def test_forwarded_for_ignored_without_trusted_proxy(self):
        self.client.get("/api/vault/recent/", HTTP_AUTHORIZATION=f"Bearer {READ_KEY}", HTTP_X_FORWARDED_FOR="1.2.3.4")
        self.assertEqual(AccessLog.objects.get().ip, "127.0.0.1")
