from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import platform_review
import dynamic_categories as dc

from legacy_migration import migrate_legacy_workspace
from platform_store import PlatformStore
from platform_review import PlatformReviewWorker, parse_review_result, review_failure
from publication import public_markdown, snapshot_fingerprint
from serve import create_app, create_server


class Client:
    def __init__(self, base: str):
        parsed = urlsplit(base)
        self.host = parsed.hostname
        self.port = parsed.port
        self.origin = base
        self.cookie = ""
        self.csrf = ""

    def request(self, method: str, path: str, body: dict | None = None, *, key: str = "test-key"):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Accept": "application/json"}
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            headers.update({"Content-Type": "application/json", "Origin": self.origin, "Idempotency-Key": key})
            if self.csrf:
                headers["X-CSRF-Token"] = self.csrf
        if self.cookie:
            headers["Cookie"] = self.cookie
        connection.request(method, path, body=raw, headers=headers)
        response = connection.getresponse()
        text = response.read().decode("utf-8")
        payload = json.loads(text) if response.getheader("Content-Type", "").startswith("application/json") else text
        cookie = response.getheader("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";", 1)[0]
        if isinstance(payload, dict) and payload.get("csrf_token"):
            self.csrf = payload["csrf_token"]
        connection.close()
        return response.status, payload

    def register(self, email: str, nickname: str):
        return self.request("POST", "/api/auth/register", {
            "email": email, "nickname": nickname, "password": "correct-horse-123",
        })


@pytest.fixture
def multi_server(tmp_path: Path):
    (tmp_path / "viewer" / "dist").mkdir(parents=True)
    (tmp_path / "viewer" / "dist" / "index.html").write_text("<!doctype html><main id='root'></main>", encoding="utf-8")
    app = create_app(tmp_path, tmp_path / "viewer", start_worker=False, multi_user=True)
    server = create_server(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        yield app, base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        app.close()


def context_for(app, client: Client):
    return app.platform.resolve_session(client.cookie.split("=", 1)[1])


def public_taxonomy_payload(app) -> dict:
    with app.platform.connect() as db:
        row = db.execute(
            "SELECT id FROM public_categories WHERE status='active' ORDER BY created_at LIMIT 1",
        ).fetchone()
        if row is None:
            category_id = "f" * 32
            timestamp = "2026-01-01T00:00:00+00:00"
            db.execute("""
                INSERT INTO public_categories(
                    id,slug,name,normalized_name,description,status,sort_order,created_at,updated_at
                ) VALUES(?,?,?,?,?,'active',0,?,?)
            """, (category_id, "test-public", "Test Public", "test public", "", timestamp, timestamp))
        else:
            category_id = row["id"]
    return {
        "category_selection": {"kind": "existing", "id": category_id},
        "tag_selections": [],
    }


def seed_article(app, client: Client, title: str = "Shared title") -> tuple[str, str]:
    context = context_for(app, client)
    root = app.platform.workspace_root(context.workspace_root_name)
    path = root / "wiki" / "concepts" / "shared.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {title}\n\n> Category: concepts\n> Status: 词条\n\n## 它做什么\n\n{title} 的私有正文。\n\n## 怎么用\n\n仅用于测试。\n\n## 例子\n\n示例。\n\n## See Also\n",
        encoding="utf-8",
    )
    service = app.workspace_service(context)
    article = service.read_article("concepts/shared.md")
    if not article["article_id"]:
        path.write_text(
            dc.ensure_article_metadata(article["markdown"], category_id=None, status="pending"),
            encoding="utf-8",
        )
        article = service.read_article("concepts/shared.md")
    return article["path"], article["revision"]


def test_private_routes_require_session_and_static_login_shell_is_public(multi_server):
    _app, base = multi_server
    anonymous = Client(base)
    assert anonymous.request("GET", "/api/articles")[0] == 401
    assert anonymous.request("GET", "/")[0] == 200
    assert anonymous.request("GET", "/api/public/entries")[1] == []


def test_share_preview_rejects_private_or_unselected_public_sources(multi_server):
    app, base = multi_server
    author = Client(base)
    assert author.register("source-author@example.com", "Source Author")[0] == 201
    path, _revision = seed_article(app, author, "Source selection")
    context = context_for(app, author)
    article_path = app.platform.workspace_root(context.workspace_root_name) / "wiki" / path
    markdown = article_path.read_text(encoding="utf-8")
    markdown = markdown.replace(
        "> Status: 词条",
        "> Status: 词条\n> Sources: https://example.com/public; http://127.0.0.1/private",
    )
    article_path.write_text(markdown, encoding="utf-8")
    article = app.workspace_service(context).read_article(path)
    base_payload = {
        "article_path": path, "source_revision": article["revision"], "attribution": "nickname",
        "source_urls": ["http://127.0.0.1/private"],
        **public_taxonomy_payload(app),
    }
    status, _ = author.request("POST", "/api/share-previews", base_payload, key="private-source")
    assert status == 422
    status, _ = author.request("POST", "/api/share-previews", {
        **base_payload, "source_urls": ["https://example.com/not-selected"],
    }, key="unselected-source")
    assert status == 422
    status, preview = author.request("POST", "/api/share-previews", {
        **base_payload, "source_urls": ["https://example.com/public"],
    }, key="public-source")
    assert status == 201
    assert preview["snapshot"]["public_sources"][0]["url"] == "https://example.com/public"


def test_two_users_isolate_files_tasks_idempotency_and_model_secrets(multi_server):
    app, base = multi_server
    alice, bob = Client(base), Client(base)
    assert alice.register("alice@example.com", "Alice")[0] == 201
    assert bob.register("bob@example.com", "Bob")[0] == 201
    seed_article(app, alice, "Alice secret")

    assert alice.request("GET", "/api/articles")[1][0]["title"] == "Alice secret"
    assert bob.request("GET", "/api/articles")[1] == []
    assert bob.request("GET", "/api/article?path=concepts/shared.md")[0] == 404

    model = {"provider": "openai-compatible", "base_url": "http://models.example.test/v1", "api_key": "alice-private-key", "model": "alpha"}
    assert alice.request("POST", "/api/settings/model", model, key="same-key")[0] == 200
    assert bob.request("POST", "/api/settings/model", {**model, "api_key": "bob-private-key", "model": "beta"}, key="same-key")[0] == 200
    assert alice.request("GET", "/api/status")[1]["model"] == "alpha"
    assert bob.request("GET", "/api/status")[1]["model"] == "beta"
    stored = (app.platform.state_root / "platform.sqlite3").read_bytes()
    assert b"alice-private-key" not in stored and b"bob-private-key" not in stored

    alice_service = app.workspace_service(context_for(app, alice))
    private_task, _ = alice_service.state.enqueue_task("supplement", "tenant-only", {"keyword": "tenant-only"})
    assert alice.request("GET", f"/api/tasks/{private_task['id']}")[0] == 200
    assert bob.request("GET", f"/api/tasks/{private_task['id']}")[0] == 404
    operation = alice_service.apply_meta("concepts/shared.md", category="concepts", status="草稿")
    assert alice.request("GET", f"/api/operations?id={operation['operation_id']}")[0] == 200
    assert bob.request("GET", f"/api/operations?id={operation['operation_id']}")[0] == 404

    rejected, _ = alice.request("POST", "/api/settings/model", {
        "provider": "ollama", "base_url": "http://127.0.0.1:11434/v1", "api_key": "", "model": "private-target",
    }, key="reject-private-model")
    assert rejected == 422
    injected, _ = alice.request("POST", "/api/generate", {"keyword": "x", "workspace_id": context_for(app, bob).workspace_id}, key="inject-workspace")
    assert injected == 400


def test_dynamic_category_article_and_reconciliation_ids_are_tenant_scoped(multi_server):
    app, base = multi_server
    alice, bob = Client(base), Client(base)
    assert alice.register("category-alice@example.com", "Category Alice")[0] == 201
    assert bob.register("category-bob@example.com", "Category Bob")[0] == 201
    seed_article(app, alice, "Alice taxonomy")
    alice_service = app.workspace_service(context_for(app, alice))
    article = alice_service.read_article("concepts/shared.md")
    category = next(item for item in alice_service.categories() if item["directory_name"] == "concepts")
    external = alice_service.root / "wiki" / "AliceExternal"
    external.mkdir()
    reconciliation = next(item for item in alice_service.scan_reconciliation()["items"] if item["kind"] == "new_directory")

    status, _ = bob.request("POST", "/api/classifications/preview", {
        "selections": [{
            "article_id": article["article_id"],
            "article_revision": article["revision"],
            "decision": "existing",
            "category_id": category["category_id"],
            "tags": [],
        }],
    }, key="cross-article")
    assert status == 404
    assert bob.request("POST", "/api/categories/preview", {
        "action": "archive", "category_id": category["category_id"],
    }, key="cross-category")[0] == 404
    assert bob.request("POST", "/api/reconciliation/preview", {
        "reconciliation_id": reconciliation["id"], "decision": "adopt",
    }, key="cross-reconciliation")[0] == 404
    for path, payload in (
        ("/api/admin/public-categories", {"name": "Bypass category"}),
        ("/api/admin/public-tags", {"name": "Bypass tag"}),
        ("/api/admin/public-category-mappings", {"private_name": "Private"}),
        (f"/api/admin/public-entries/{'f' * 32}/taxonomy", {"category_id": "f" * 32}),
        (f"/api/admin/public-categories/{'f' * 32}/update", {
            "slug": "cannot-create", "name": "Cannot Create", "description": "", "status": "active", "sort_order": 0,
        }),
        (f"/api/admin/public-tags/{'f' * 32}/update", {
            "slug": "cannot-create", "name": "Cannot Create", "status": "active",
        }),
    ):
        assert alice.request("POST", path, payload, key=f"removed-{path}")[0] == 404


def test_logout_revoke_all_and_role_change_take_effect_immediately(multi_server):
    app, base = multi_server
    alice = Client(base)
    _, registered = alice.register("alice@example.com", "Alice")
    assert registered["user"]["role"] == "admin"
    context = context_for(app, alice)
    app.platform.set_role(context.user_id, "user")
    assert alice.request("GET", "/api/admin/submissions")[0] == 403
    assert alice.request("POST", "/api/auth/sessions/revoke-all", {}, key="revoke")[0] == 200
    assert alice.request("GET", "/api/articles")[0] == 401


def test_snapshot_ai_admin_publish_with_self_review_and_idor_guards(multi_server):
    app, base = multi_server
    alice, bob = Client(base), Client(base)
    alice.register("alice@example.com", "Alice")
    bob.register("bob@example.com", "Bob")
    article_path, revision = seed_article(app, alice, "Immutable article")
    assert alice.request("GET", f"/api/article?path={article_path}")[1]["publication"]["state"] == "not_published"

    status, preview = alice.request("POST", "/api/share-previews", {
        "article_path": article_path, "source_revision": revision, "attribution": "nickname",
        **public_taxonomy_payload(app),
    }, key="preview")
    assert status == 201
    assert "Article-ID" not in preview["snapshot"]["markdown"]
    assert "Category-ID" not in preview["snapshot"]["markdown"]
    assert "Classification-Updated" not in preview["snapshot"]["markdown"]
    status, submission = alice.request("POST", "/api/submissions", {"preview_id": preview["preview_id"]}, key="submit")
    assert status == 201 and submission["status"] == "ai_queued"
    snapshot_hash = submission["content_hash"]
    publication = alice.request("GET", f"/api/article?path={article_path}")[1]["publication"]
    assert publication["state"] == "submitted"
    assert publication["submission_id"] == submission["id"]
    assert publication["submission_matches_current"] is True

    # A later private edit cannot mutate the immutable submitted snapshot.
    context = context_for(app, alice)
    private_file = app.platform.workspace_root(context.workspace_root_name) / "wiki" / article_path
    private_file.write_text(private_file.read_text(encoding="utf-8") + "\nPrivate update.\n", encoding="utf-8")
    assert alice.request("GET", f"/api/submissions/{submission['id']}")[1]["content_hash"] == snapshot_hash
    assert bob.request("GET", f"/api/submissions/{submission['id']}")[0] == 404
    changed_during_review = alice.request("GET", f"/api/article?path={article_path}")[1]["publication"]
    assert changed_during_review["state"] == "submitted"
    assert changed_during_review["submission_matches_current"] is False

    app.platform.ai_decide(submission["id"], "pass", {"summary": "structure accepted"})
    # The bootstrap Admin may review their own AI-approved immutable submission.
    decision = {"decision": "approve", "reason": "meets the publishing standard"}
    status, approved = alice.request("POST", f"/api/admin/submissions/{submission['id']}/decision", decision, key="self-review")
    assert status == 200 and approved["status"] == "approved"
    with app.platform.connect() as db:
        audit = db.execute(
            "SELECT detail_json FROM audit_events WHERE action='submission.approve' AND object_id=? ORDER BY created_at DESC LIMIT 1",
            (submission["id"],),
        ).fetchone()
    assert json.loads(audit["detail_json"])["self_review"] is True

    bob_context = context_for(app, bob)
    app.platform.set_role(bob_context.user_id, "admin", actor_id=context.user_id)
    public_id = approved["public_entry_id"]
    public_before = Client(base).request("GET", f"/api/public/entries/{public_id}")[1]
    assert public_before["content_hash"] == snapshot_hash
    update_available = alice.request("GET", f"/api/article?path={article_path}")[1]["publication"]
    assert update_available["state"] == "update_available"
    assert update_available["public_entry_id"] == public_id
    assert update_available["public_version"] == 1
    reporter = Client(base)
    report_status, report = reporter.request("POST", f"/api/public/entries/{public_id}/reports", {"reason_code": "content_concern", "detail": "please verify"}, key="report")
    assert report_status == 201
    assert any(row["id"] == report["id"] for row in bob.request("GET", "/api/admin/reports")[1])
    resolved = bob.request("POST", f"/api/admin/reports/{report['id']}/decision", {"action": "dismiss", "reason": "verified"}, key="dismiss-report")
    assert resolved[0] == 200 and resolved[1]["status"] == "resolved"

    # A changed private article becomes a new immutable public revision and two admins cannot both decide it.
    changed = app.workspace_service(context).read_article(article_path)
    next_preview = alice.request("POST", "/api/share-previews", {
        "article_path": article_path, "source_revision": changed["revision"], "attribution": "nickname",
        **public_taxonomy_payload(app),
    }, key="preview-v2")[1]
    next_submission = alice.request("POST", "/api/submissions", {"preview_id": next_preview["preview_id"]}, key="submit-v2")[1]
    update_pending = alice.request("GET", f"/api/article?path={article_path}")[1]["publication"]
    assert update_pending["state"] == "update_pending"
    assert update_pending["submission_id"] == next_submission["id"]
    app.platform.ai_decide(next_submission["id"], "pass", {"summary": "accepted v2"})
    carol = Client(base); carol.register("carol@example.com", "Carol")
    carol_context = context_for(app, carol)
    app.platform.set_role(carol_context.user_id, "admin", actor_id=context.user_id)
    outcomes = []
    barrier = threading.Barrier(2)
    def review(client: Client, key: str):
        barrier.wait()
        outcomes.append(client.request("POST", f"/api/admin/submissions/{next_submission['id']}/decision", decision, key=key)[0])
    threads = [threading.Thread(target=review, args=(bob, "approve-bob")), threading.Thread(target=review, args=(carol, "approve-carol"))]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sorted(outcomes) == [200, 409]
    public_v2 = Client(base).request("GET", f"/api/public/entries/{public_id}")[1]
    assert public_v2["version"] == 2 and public_v2["content_hash"] == next_submission["content_hash"]
    published = alice.request("GET", f"/api/article?path={article_path}")[1]["publication"]
    assert published["state"] == "published"
    assert published["public_version"] == 2
    duplicate_status, _ = alice.request("POST", "/api/share-previews", {
        "article_path": article_path, "source_revision": changed["revision"], "attribution": "nickname",
        **public_taxonomy_payload(app),
    }, key="duplicate-published-preview")
    assert duplicate_status == 409

    assert alice.request("POST", f"/api/submissions/{next_submission['id']}/withdraw", {}, key="withdraw")[0] == 409
    assert Client(base).request("GET", f"/api/public/entries/{public_id}")[0] == 200


def test_takedown_notifications_author_reapply_and_admin_relist(multi_server):
    app, base = multi_server
    admin, author = Client(base), Client(base)
    admin.register("admin@example.com", "Admin")
    author.register("author@example.com", "Author")
    article_path, revision = seed_article(app, author, "Moderated article")

    preview = author.request("POST", "/api/share-previews", {
        "article_path": article_path, "source_revision": revision, "attribution": "nickname",
        **public_taxonomy_payload(app),
    }, key="moderation-preview")[1]
    submission = author.request("POST", "/api/submissions", {
        "preview_id": preview["preview_id"],
    }, key="moderation-submit")[1]
    app.platform.ai_decide(submission["id"], "pass", {"summary": "accepted"})
    approved = admin.request("POST", f"/api/admin/submissions/{submission['id']}/decision", {
        "decision": "approve", "reason": "publish",
    }, key="moderation-approve")[1]
    public_id = approved["public_entry_id"]
    assert any(row["id"] == public_id for row in admin.request(
        "GET", "/api/admin/public-entries?status=published",
    )[1])

    report = Client(base).request("POST", f"/api/public/entries/{public_id}/reports", {
        "reason_code": "content_concern", "detail": "needs correction",
    }, key="moderation-report")[1]
    removed = admin.request("POST", f"/api/admin/reports/{report['id']}/decision", {
        "action": "remove", "reason": "事实表述需要修正",
    }, key="remove-from-report")
    assert removed[0] == 200
    assert Client(base).request("GET", f"/api/public/entries/{public_id}")[0] == 404

    notifications = author.request("GET", "/api/notifications")[1]
    notice = next(row for row in notifications if row["object_id"] == public_id and row["kind"] == "public_removed")
    assert "事实表述需要修正" in notice["message"] and notice["read_at"] is None
    assert admin.request("POST", f"/api/notifications/{notice['id']}/read", {}, key="notification-idor")[0] == 404
    assert author.request("POST", f"/api/notifications/{notice['id']}/read", {}, key="notification-read")[1]["read_at"]

    removed_state = author.request("GET", f"/api/article?path={article_path}")[1]["publication"]
    assert removed_state["state"] == "removed"
    assert removed_state["moderation_reason"] == "事实表述需要修正"
    unchanged_status = author.request("POST", "/api/share-previews", {
        "article_path": article_path, "source_revision": revision, "attribution": "nickname",
        **public_taxonomy_payload(app),
    }, key="unchanged-relist")[0]
    assert unchanged_status == 409
    removed_rows = admin.request("GET", "/api/admin/public-entries?status=removed_by_admin")[1]
    assert removed_rows[0]["id"] == public_id
    assert removed_rows[0]["moderation_reason"] == "事实表述需要修正"
    assert author.request("GET", "/api/admin/public-entries?status=removed_by_admin")[0] == 403

    context = context_for(app, author)
    private_file = app.platform.workspace_root(context.workspace_root_name) / "wiki" / article_path
    private_file.write_text(private_file.read_text(encoding="utf-8") + "\n修正后的正文。\n", encoding="utf-8")
    changed = app.workspace_service(context).read_article(article_path)
    assert author.request("GET", f"/api/article?path={article_path}")[1]["publication"]["state"] == "relist_available"
    next_preview = author.request("POST", "/api/share-previews", {
        "article_path": article_path, "source_revision": changed["revision"], "attribution": "nickname",
        **public_taxonomy_payload(app),
    }, key="relist-preview")[1]
    next_submission = author.request("POST", "/api/submissions", {
        "preview_id": next_preview["preview_id"],
    }, key="relist-submit")[1]
    assert author.request("GET", f"/api/article?path={article_path}")[1]["publication"]["state"] == "relist_pending"
    app.platform.ai_decide(next_submission["id"], "pass", {"summary": "corrected"})
    admin.request("POST", f"/api/admin/submissions/{next_submission['id']}/decision", {
        "decision": "approve", "reason": "correction accepted",
    }, key="relist-approve")
    assert Client(base).request("GET", f"/api/public/entries/{public_id}")[1]["version"] == 2

    assert admin.request("POST", f"/api/admin/public-entries/{public_id}/remove", {
        "reason": "Admin 主动下架测试",
    }, key="manual-remove")[1]["status"] == "removed_by_admin"
    assert author.request("POST", f"/api/admin/public-entries/{public_id}/relist", {
        "reason": "越权恢复",
    }, key="forbidden-relist")[0] == 403
    assert admin.request("POST", f"/api/admin/public-entries/{public_id}/relist", {
        "reason": "复核后恢复",
    }, key="manual-relist")[1]["status"] == "published"
    assert Client(base).request("GET", f"/api/public/entries/{public_id}")[0] == 200
    kinds = [row["kind"] for row in author.request("GET", "/api/notifications")[1] if row["object_id"] == public_id]
    assert kinds.count("public_removed") == 2
    assert kinds.count("public_relisted") == 2


def test_non_loopback_server_binding_is_refused(multi_server):
    app, _base = multi_server
    with pytest.raises(ValueError, match="loopback-only"):
        create_server(app, host="0.0.0.0", port=0)


def test_legacy_migration_hashes_backups_encrypts_model_and_rolls_back(tmp_path: Path):
    (tmp_path / "wiki" / "concepts").mkdir(parents=True)
    (tmp_path / "raw" / "local").mkdir(parents=True)
    (tmp_path / ".wiki-state").mkdir()
    article = tmp_path / "wiki" / "concepts" / "legacy.md"
    article.write_text("# Legacy\n\n> Category: concepts\n> Status: 词条\n", encoding="utf-8")
    (tmp_path / "raw" / "local" / "seed.md").write_text("# Seed\n", encoding="utf-8")
    import sqlite3
    with sqlite3.connect(tmp_path / ".wiki-state" / "state.sqlite3") as db:
        db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO tasks VALUES('legacy-task')")
    (tmp_path / ".wiki-state" / "model-settings.json").write_text(json.dumps({
        "provider": "openai-compatible", "base_url": "http://legacy.example/v1",
        "api_key": "legacy-secret", "model": "legacy-model",
    }), encoding="utf-8")

    platform = PlatformStore(tmp_path)
    user, _recovery = platform.register("owner@example.com", "Owner", "correct-horse-123")
    source_hash = article.read_bytes()
    with pytest.raises(RuntimeError, match="injected"):
        migrate_legacy_workspace(platform, user["id"], fail_after=1)
    target = platform.workspace_root(user["workspace_root_name"])
    assert article.read_bytes() == source_hash
    assert not (target / "wiki" / "concepts" / "legacy.md").exists()

    manifest = migrate_legacy_workspace(platform, user["id"])
    assert manifest["status"] == "committed"
    assert (target / "wiki" / "concepts" / "legacy.md").read_bytes() == source_hash
    assert (tmp_path / manifest["backup"] / "wiki" / "concepts" / "legacy.md").read_bytes() == source_hash
    assert platform.load_model(user["workspace_id"])["api_key"] == "legacy-secret"
    assert b"legacy-secret" not in (platform.state_root / "platform.sqlite3").read_bytes()


def test_platform_ai_worker_reads_snapshot_only_and_recovers_to_admin_queue(tmp_path: Path):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("owner@example.com", "Owner", "correct-horse-123")
    token, context = platform.create_session(user["id"])
    assert token
    snapshot = {"title": "Public candidate", "category": "concepts", "markdown": "# Public candidate\n\nBody", "attribution": "Owner"}
    article_id = "a" * 32
    preview = platform.create_preview(
        context, "concepts/private.md", "revision-1", article_id, snapshot_fingerprint(snapshot), snapshot,
    )
    submission = platform.submit_preview(context, preview["preview_id"])
    seen = []
    worker = PlatformReviewWorker(platform, lambda value: seen.append(value) or {"decision": "pass", "summary": "accepted"})
    try:
        deadline = time.time() + 2
        while time.time() < deadline and platform.get_submission(context, submission["id"])["status"] != "pending_admin":
            time.sleep(0.02)
        result = platform.get_submission(context, submission["id"])
        assert result["status"] == "pending_admin"
        assert seen == [{
            "snapshot": {
                "title": snapshot["title"],
                "markdown": snapshot["markdown"],
                "attribution": snapshot["attribution"],
            },
            "duplicate_candidates": [],
        }]
        assert "category" not in seen[0]["snapshot"]
        assert "article_path" not in seen[0] and "workspace_id" not in seen[0]
    finally:
        worker.close()


def test_platform_ai_uses_submitter_workspace_model_without_cross_tenant_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    platform = PlatformStore(tmp_path)
    alice, _ = platform.register("alice@example.com", "Alice", "correct-horse-123")
    bob, _ = platform.register("bob@example.com", "Bob", "correct-horse-123")
    platform.save_model(alice["workspace_id"], "openai-compatible", "http://alice.example/v1", "alice-key", "alice-model")
    platform.save_model(bob["workspace_id"], "openai-compatible", "http://bob.example/v1", "bob-key", "bob-model")
    _token, bob_context = platform.create_session(bob["id"])
    snapshot = {"title": "Bob candidate", "markdown": "# Bob candidate\n\nBody"}
    article_id = "b" * 32
    preview = platform.create_preview(
        bob_context, "private/bob.md", "rev-bob", article_id, snapshot_fingerprint(snapshot), snapshot,
    )
    submission = platform.submit_preview(bob_context, preview["preview_id"])
    captured = {}

    def fake_review(value, settings):
        captured.update(snapshot=value, settings=settings)
        return {"decision": "pass", "summary": "accepted"}

    monkeypatch.setattr(platform_review, "default_reviewer", fake_review)
    worker = PlatformReviewWorker(platform)
    try:
        deadline = time.time() + 2
        while time.time() < deadline and platform.get_submission(bob_context, submission["id"])["status"] != "pending_admin":
            time.sleep(0.02)
        assert platform.get_submission(bob_context, submission["id"])["status"] == "pending_admin"
        assert captured["snapshot"]["snapshot"] == snapshot
        assert captured["snapshot"]["duplicate_candidates"] == []
        assert captured["settings"]["api_key"] == "bob-key"
        assert captured["settings"]["model"] == "bob-model"
        serialized = json.dumps(captured, ensure_ascii=False)
        assert "alice-key" not in serialized
        assert "private/bob.md" not in serialized and bob_context.workspace_id not in serialized
        with platform.connect() as db:
            attempt = db.execute(
                "SELECT provider,model,report_json FROM submission_review_attempts WHERE submission_id=?",
                (submission["id"],),
            ).fetchone()
        assert attempt["provider"] == "openai-compatible"
        assert attempt["model"] == "bob-model"
        assert "bob-key" not in attempt["report_json"]
        assert "bob.example" not in attempt["report_json"]
    finally:
        worker.close()


def test_platform_ai_retry_reads_latest_submitter_workspace_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("owner@example.com", "Owner", "correct-horse-123")
    platform.save_model(
        user["workspace_id"], "openai-compatible", "https://old.example/v1", "old-key", "old-model",
    )
    _token, context = platform.create_session(user["id"])
    snapshot = {"title": "Retry candidate", "markdown": "# Retry candidate\n\nBody"}
    preview = platform.create_preview(
        context, "private/retry.md", "rev-retry", "c" * 32, snapshot_fingerprint(snapshot), snapshot,
    )
    submission = platform.submit_preview(context, preview["preview_id"])
    seen_models: list[str] = []

    def fake_review(_value, settings):
        seen_models.append(settings["model"])
        if len(seen_models) == 1:
            return {"decision": "failed", "summary": "retry"}
        return {"decision": "pass", "summary": "accepted"}

    monkeypatch.setattr(platform_review, "default_reviewer", fake_review)
    worker = PlatformReviewWorker(platform)
    try:
        deadline = time.time() + 2
        while time.time() < deadline and platform.get_submission(context, submission["id"])["status"] != "ai_failed":
            time.sleep(0.02)
        assert platform.get_submission(context, submission["id"])["status"] == "ai_failed"

        platform.save_model(
            user["workspace_id"], "openai-compatible", "https://new.example/v1", "new-key", "new-model",
        )
        platform.retry_ai(context, submission["id"])
        worker.wake()
        deadline = time.time() + 2
        while time.time() < deadline and platform.get_submission(context, submission["id"])["status"] != "pending_admin":
            time.sleep(0.02)
        assert platform.get_submission(context, submission["id"])["status"] == "pending_admin"
        assert seen_models == ["old-model", "new-model"]
    finally:
        worker.close()


def test_platform_ai_bad_workspace_cipher_fails_one_attempt_and_worker_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    platform = PlatformStore(tmp_path)
    broken_user, _ = platform.register("broken@example.com", "Broken", "correct-horse-123")
    healthy_user, _ = platform.register("healthy@example.com", "Healthy", "correct-horse-123")
    platform.save_model(
        broken_user["workspace_id"], "openai-compatible", "https://broken.example/v1", "broken-key", "broken-model",
    )
    platform.save_model(
        healthy_user["workspace_id"], "openai-compatible", "https://healthy.example/v1", "healthy-key", "healthy-model",
    )
    _token, broken_context = platform.create_session(broken_user["id"])
    _token, healthy_context = platform.create_session(healthy_user["id"])

    def submit(context, title, article_id):
        snapshot = {"title": title, "markdown": f"# {title}\n\nBody"}
        preview = platform.create_preview(
            context, f"private/{title}.md", f"rev-{title}", article_id, snapshot_fingerprint(snapshot), snapshot,
        )
        return platform.submit_preview(context, preview["preview_id"])

    broken = submit(broken_context, "broken", "d" * 32)
    healthy = submit(healthy_context, "healthy", "e" * 32)
    with platform.connect() as db:
        db.execute(
            "UPDATE model_settings SET api_key_enc='not-valid-ciphertext' WHERE workspace_id=?",
            (broken_user["workspace_id"],),
        )
    monkeypatch.setattr(
        platform_review, "default_reviewer", lambda _value, _settings: {"decision": "pass", "summary": "ok"},
    )
    worker = PlatformReviewWorker(platform)
    try:
        deadline = time.time() + 3
        while time.time() < deadline:
            broken_state = platform.get_submission(broken_context, broken["id"])["status"]
            healthy_state = platform.get_submission(healthy_context, healthy["id"])["status"]
            if broken_state == "ai_failed" and healthy_state == "pending_admin":
                break
            time.sleep(0.02)
        assert broken_state == "ai_failed"
        assert healthy_state == "pending_admin"
        assert worker._thread.is_alive()
        with platform.connect() as db:
            attempt = db.execute(
                "SELECT * FROM submission_review_attempts WHERE submission_id=?", (broken["id"],),
            ).fetchone()
        assert attempt["status"] == "ai_failed"
        assert attempt["provider"] == "openai-compatible" and attempt["model"] == "broken-model"
        assert attempt["policy_version"] and attempt["rules_version"] and attempt["completed_at"]
        assert "not-valid-ciphertext" not in attempt["report_json"]
    finally:
        worker.close()


@pytest.mark.parametrize("secret_field", ["summary", "extra", "issue"])
def test_platform_ai_rejects_workspace_secret_echo_before_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret_field: str,
):
    platform = PlatformStore(tmp_path)
    admin, _ = platform.register("admin@example.com", "Admin", "correct-horse-123")
    author, _ = platform.register("author@example.com", "Author", "correct-horse-123")
    base_url, api_key = "https://private-model.example/v1", "private-api-key-value"
    platform.save_model(author["workspace_id"], "openai-compatible", base_url, api_key, "private-model")
    _token, admin_context = platform.create_session(admin["id"])
    _token, author_context = platform.create_session(author["id"])
    snapshot = {"title": "Secret echo", "markdown": "# Secret echo\n\nBody"}
    preview = platform.create_preview(
        author_context, "private/echo.md", "rev-echo", "f" * 32, snapshot_fingerprint(snapshot), snapshot,
    )
    submission = platform.submit_preview(author_context, preview["preview_id"])

    def malicious(_value, _settings):
        result = {"decision": "pass", "summary": "looks safe", "issues": []}
        if secret_field == "summary":
            result["summary"] = f"credential={api_key}"
        elif secret_field == "extra":
            result["extra"] = {"nested": [base_url]}
        else:
            result["issues"] = [{"code": "echo", "location": base_url, "explanation": api_key}]
        return result

    monkeypatch.setattr(platform_review, "default_reviewer", malicious)
    worker = PlatformReviewWorker(platform)
    try:
        deadline = time.time() + 2
        while time.time() < deadline and platform.get_submission(author_context, submission["id"])["status"] != "ai_failed":
            time.sleep(0.02)
        author_view = platform.get_submission(author_context, submission["id"])
        admin_view = platform.admin_get(admin_context, submission["id"])
        with platform.connect() as db:
            stored = db.execute(
                "SELECT ai_report_json FROM submissions WHERE id=?", (submission["id"],),
            ).fetchone()[0]
            attempt = db.execute(
                "SELECT * FROM submission_review_attempts WHERE submission_id=?", (submission["id"],),
            ).fetchone()
        serialized = json.dumps([author_view, admin_view, stored, dict(attempt)], ensure_ascii=False)
        assert author_view["status"] == "ai_failed"
        assert api_key not in serialized and base_url not in serialized
        assert attempt["provider"] == "openai-compatible" and attempt["model"] == "private-model"
        assert attempt["policy_version"] and attempt["rules_version"]
        assert set(json.loads(stored)) == {
            "decision", "summary", "issues", "policy_version", "provider", "model", "rules_version",
        }
    finally:
        worker.close()


def test_platform_ai_projects_real_public_snapshot_before_review(tmp_path: Path):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("projection@example.com", "Projection", "correct-horse-123")
    _token, context = platform.create_session(user["id"])
    source = """# Projection

> Article-ID: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
> Category-ID: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
> Classification: confirmed
> Category: PRIVATE_CATEGORY_CANARY
> Tags: PRIVATE_TAG_CANARY
> Raw: raw/private/canary.md

Visible body.

> Category: this body quote remains public prose
"""
    projected = public_markdown(source)
    assert "PRIVATE_CATEGORY_CANARY" in projected and "PRIVATE_TAG_CANARY" in projected
    snapshot = {
        "title": "Projection", "category": "PRIVATE_CATEGORY_CANARY",
        "markdown": projected, "summary": "Visible body",
    }
    preview = platform.create_preview(
        context, "private/projection.md", "rev-projection", "a" * 32,
        snapshot_fingerprint(snapshot), snapshot,
    )
    submission = platform.submit_preview(context, preview["preview_id"])
    claimed = platform.claim_ai_submission()
    assert claimed and claimed["id"] == submission["id"]
    serialized = json.dumps(claimed["review_input"], ensure_ascii=False)
    for private in (
        "PRIVATE_CATEGORY_CANARY", "PRIVATE_TAG_CANARY", "raw/private/canary.md",
        "Article-ID", "Category-ID", "Classification",
    ):
        assert private not in serialized
    assert "this body quote remains public prose" in serialized


@pytest.mark.parametrize("collision", ["api_key", "base_url"])
def test_platform_ai_rejects_model_name_secret_collision_at_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collision: str,
):
    platform = PlatformStore(tmp_path)
    admin, _ = platform.register("admin-collision@example.com", "Admin", "correct-horse-123")
    author, _ = platform.register("author-collision@example.com", "Author", "correct-horse-123")
    _admin_token, admin_context = platform.create_session(admin["id"])
    _author_token, author_context = platform.create_session(author["id"])
    base_url, api_key = "https://collision.example/v1", "collision-api-key"
    secret = api_key if collision == "api_key" else base_url
    unsafe_model = f"prefix-{secret}-suffix"
    with pytest.raises(ValueError, match="must not match"):
        platform.save_model(author["workspace_id"], "openai-compatible", base_url, api_key, unsafe_model)
    platform.save_model(author["workspace_id"], "openai-compatible", base_url, api_key, "safe-model")
    with platform.connect() as db:
        db.execute("UPDATE model_settings SET model=? WHERE workspace_id=?", (unsafe_model, author["workspace_id"]))
    snapshot = {"title": "Collision", "markdown": "# Collision\n\nBody"}
    preview = platform.create_preview(
        author_context, "private/collision.md", "rev-collision", "b" * 32,
        snapshot_fingerprint(snapshot), snapshot,
    )
    submission = platform.submit_preview(author_context, preview["preview_id"])
    calls = []
    monkeypatch.setattr(platform_review, "default_reviewer", lambda *_args: calls.append(True) or {"decision": "pass"})
    worker = PlatformReviewWorker(platform)
    try:
        deadline = time.time() + 2
        while time.time() < deadline and platform.get_submission(author_context, submission["id"])["status"] != "ai_failed":
            time.sleep(0.02)
        author_view = platform.get_submission(author_context, submission["id"])
        admin_view = platform.admin_get(admin_context, submission["id"])
        with platform.connect() as db:
            stored = db.execute(
                "SELECT ai_report_json,ai_model FROM submissions WHERE id=?", (submission["id"],),
            ).fetchone()
            attempt = db.execute(
                "SELECT model,report_json FROM submission_review_attempts WHERE submission_id=?",
                (submission["id"],),
            ).fetchone()
        serialized = json.dumps([author_view, admin_view, dict(stored), dict(attempt)], ensure_ascii=False)
        assert not calls and author_view["status"] == "ai_failed"
        assert secret not in serialized
        assert platform.list_public() == []
    finally:
        worker.close()


def test_platform_ai_projection_failure_settles_attempt_and_worker_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("projection-failure@example.com", "Owner", "correct-horse-123")
    platform.save_model(user["workspace_id"], "openai-compatible", "https://model.example/v1", "key", "model")
    _token, context = platform.create_session(user["id"])

    def submit(title: str, article_id: str):
        snapshot = {"title": title, "markdown": f"# {title}\n\nBody"}
        preview = platform.create_preview(
            context, f"private/{title}.md", f"rev-{title}", article_id,
            snapshot_fingerprint(snapshot), snapshot,
        )
        return platform.submit_preview(context, preview["preview_id"])

    broken, healthy = submit("broken-projection", "c" * 32), submit("healthy-projection", "d" * 32)
    original = platform.duplicate_candidates
    calls = 0

    def flaky(snapshot):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("projection failed")
        return original(snapshot)

    monkeypatch.setattr(platform, "duplicate_candidates", flaky)
    monkeypatch.setattr(platform_review, "default_reviewer", lambda *_args: {"decision": "pass", "summary": "ok"})
    worker = PlatformReviewWorker(platform)
    try:
        deadline = time.time() + 3
        while time.time() < deadline:
            broken_status = platform.get_submission(context, broken["id"])["status"]
            healthy_status = platform.get_submission(context, healthy["id"])["status"]
            if broken_status == "ai_failed" and healthy_status == "pending_admin":
                break
            time.sleep(0.02)
        assert (broken_status, healthy_status) == ("ai_failed", "pending_admin")
        with platform.connect() as db:
            rows = db.execute(
                "SELECT status,completed_at FROM submission_review_attempts WHERE submission_id=?",
                (broken["id"],),
            ).fetchall()
        assert len(rows) == 1 and rows[0]["status"] == "ai_failed" and rows[0]["completed_at"]
        assert worker._thread.is_alive()
    finally:
        worker.close()


def test_platform_ai_finalize_retries_without_recalling_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("finalize@example.com", "Owner", "correct-horse-123")
    platform.save_model(user["workspace_id"], "openai-compatible", "https://model.example/v1", "key", "model")
    _token, context = platform.create_session(user["id"])
    snapshot = {"title": "Finalize", "markdown": "# Finalize\n\nBody"}
    preview = platform.create_preview(
        context, "private/finalize.md", "rev-finalize", "e" * 32,
        snapshot_fingerprint(snapshot), snapshot,
    )
    submission = platform.submit_preview(context, preview["preview_id"])
    review_calls, finalize_calls = [], []
    monkeypatch.setattr(
        platform_review, "default_reviewer",
        lambda *_args: review_calls.append(True) or {"decision": "pass", "summary": "ok"},
    )
    original = platform.ai_decide

    def flaky_finalize(*args, **kwargs):
        finalize_calls.append(True)
        if len(finalize_calls) == 1:
            raise RuntimeError("temporary finalize failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(platform, "ai_decide", flaky_finalize)
    worker = PlatformReviewWorker(platform)
    try:
        deadline = time.time() + 3
        while time.time() < deadline and platform.get_submission(context, submission["id"])["status"] != "pending_admin":
            time.sleep(0.02)
        assert platform.get_submission(context, submission["id"])["status"] == "pending_admin"
        assert len(review_calls) == 1 and len(finalize_calls) >= 2
    finally:
        worker.close()


@pytest.mark.parametrize("action,expected", [("suspend", "ai_failed"), ("delete", "withdrawn")])
def test_workspace_lifecycle_fences_claimed_ai_review(tmp_path: Path, action: str, expected: str):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("owner@example.com", "Owner", "correct-horse-123")
    token, personal_context = platform.create_session(user["id"])
    team = platform.create_team(personal_context, "Review Team")
    team_context = platform.switch_workspace(token, personal_context, team["id"])
    platform.save_model(team["id"], "openai-compatible", "https://team.example/v1", "team-key", "team-model")
    with platform.connect() as db:
        db.execute(
            "UPDATE model_settings SET model=? WHERE workspace_id=?",
            ("prefix-team-key-suffix", team["id"]),
        )
    snapshot = {"title": "Lifecycle", "markdown": "# Lifecycle\n\nBody"}
    preview = platform.create_preview(
        team_context, "private/lifecycle.md", "rev-lifecycle", "1" * 32,
        snapshot_fingerprint(snapshot), snapshot,
    )
    submission = platform.submit_preview(team_context, preview["preview_id"])
    claimed = platform.claim_ai_submission()
    assert claimed and claimed["id"] == submission["id"]
    queued_snapshot = {"title": "Queued lifecycle", "markdown": "# Queued lifecycle\n\nBody"}
    queued_preview = platform.create_preview(
        team_context, "private/queued-lifecycle.md", "rev-queued", "2" * 32,
        snapshot_fingerprint(queued_snapshot), queued_snapshot,
    )
    queued = platform.submit_preview(team_context, queued_preview["preview_id"])
    platform.change_workspace_lifecycle(team_context, team["id"], "suspend")
    if action == "delete":
        platform.change_workspace_lifecycle(team_context, team["id"], "delete")
    stale = platform.ai_decide(
        submission["id"], "pass",
        {
            "summary": "late", "issues": [], "policy_version": platform_review.REVIEW_POLICY_VERSION,
            "provider": "openai-compatible", "model": "team-model",
            "rules_version": platform_review.REVIEW_POLICY_VERSION,
        },
        expected_attempt=claimed["attempt"],
    )
    assert stale["stale"] is True
    with platform.connect() as db:
        row = db.execute(
            "SELECT status,ai_model,ai_report_json FROM submissions WHERE id=?", (submission["id"],),
        ).fetchone()
        queued_row = db.execute(
            "SELECT status,ai_model,ai_report_json FROM submissions WHERE id=?", (queued["id"],),
        ).fetchone()
        attempt = db.execute(
            "SELECT status,model,report_json,completed_at FROM submission_review_attempts WHERE submission_id=?",
            (submission["id"],),
        ).fetchone()
    assert row["status"] == expected
    assert queued_row["status"] == expected
    assert attempt["status"] == "ai_failed" and attempt["completed_at"]
    assert "team-key" not in json.dumps([dict(row), dict(queued_row), dict(attempt)], ensure_ascii=False)
    assert platform.claim_ai_submission() is None


def test_platform_startup_reconciles_inactive_and_interrupted_reviews(tmp_path: Path):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("restart@example.com", "Owner", "correct-horse-123")
    token, personal = platform.create_session(user["id"])
    team = platform.create_team(personal, "Restart Team")
    team_context = platform.switch_workspace(token, personal, team["id"])
    platform.save_model(team["id"], "openai-compatible", "https://team.example/v1", "key", "model")
    with platform.connect() as db:
        db.execute("UPDATE model_settings SET model=? WHERE workspace_id=?", ("prefix-key-suffix", team["id"]))

    def submit(title: str, article_id: str):
        snapshot = {"title": title, "markdown": f"# {title}\n\nBody"}
        preview = platform.create_preview(
            team_context, f"private/{title}.md", f"rev-{title}", article_id,
            snapshot_fingerprint(snapshot), snapshot,
        )
        return platform.submit_preview(team_context, preview["preview_id"])

    interrupted = submit("interrupted", "f" * 32)
    claimed = platform.claim_ai_submission()
    assert claimed and claimed["id"] == interrupted["id"]
    restarted = PlatformStore(tmp_path)
    with restarted.connect() as db:
        submission = db.execute("SELECT status FROM submissions WHERE id=?", (interrupted["id"],)).fetchone()
        old_attempt = db.execute(
            "SELECT status,model,report_json,completed_at FROM submission_review_attempts WHERE submission_id=? AND attempt=1",
            (interrupted["id"],),
        ).fetchone()
    assert submission["status"] == "ai_queued"
    assert old_attempt["status"] == "ai_failed" and old_attempt["completed_at"]
    assert "key" not in json.dumps(dict(old_attempt), ensure_ascii=False)
    next_claim = restarted.claim_ai_submission()
    assert next_claim and next_claim["attempt"] == 2

    queued = submit("inactive-queued", "1" * 32)
    pending = submit("inactive-pending", "2" * 32)
    with platform.connect() as db:
        db.execute("UPDATE submissions SET status='pending_admin' WHERE id=?", (pending["id"],))
        db.execute("UPDATE workspaces SET status='suspended' WHERE id=?", (team["id"],))
    reconciled = PlatformStore(tmp_path)
    with reconciled.connect() as db:
        statuses = {
            row["id"]: row["status"]
            for row in db.execute("SELECT id,status FROM submissions WHERE id IN (?,?)", (queued["id"], pending["id"]))
        }
    assert statuses == {queued["id"]: "ai_failed", pending["id"]: "ai_failed"}


def test_platform_startup_recovers_review_without_model_settings(tmp_path: Path):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("no-model-restart@example.com", "Owner", "correct-horse-123")
    _token, context = platform.create_session(user["id"])
    snapshot = {"title": "No model", "markdown": "# No model\n\nBody"}
    preview = platform.create_preview(
        context, "private/no-model.md", "rev-no-model", "4" * 32,
        snapshot_fingerprint(snapshot), snapshot,
    )
    submission = platform.submit_preview(context, preview["preview_id"])
    claimed = platform.claim_ai_submission()
    assert claimed and claimed["id"] == submission["id"]

    restarted = PlatformStore(tmp_path)
    with restarted.connect() as db:
        current = db.execute("SELECT status FROM submissions WHERE id=?", (submission["id"],)).fetchone()
        attempt = db.execute(
            "SELECT status,model,completed_at FROM submission_review_attempts WHERE submission_id=? AND attempt=1",
            (submission["id"],),
        ).fetchone()
    assert current["status"] == "ai_queued"
    assert attempt["status"] == "ai_failed" and attempt["model"] is None and attempt["completed_at"]


def test_platform_startup_closes_orphan_running_attempts(tmp_path: Path):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("orphan-attempt@example.com", "Owner", "correct-horse-123")
    _token, context = platform.create_session(user["id"])
    snapshot = {"title": "Orphan", "markdown": "# Orphan\n\nBody"}
    preview = platform.create_preview(
        context, "private/orphan.md", "rev-orphan", "5" * 32,
        snapshot_fingerprint(snapshot), snapshot,
    )
    submission = platform.submit_preview(context, preview["preview_id"])
    assert platform.claim_ai_submission()
    with platform.connect() as db:
        db.execute("UPDATE submissions SET status='ai_failed' WHERE id=?", (submission["id"],))

    restarted = PlatformStore(tmp_path)
    with restarted.connect() as db:
        current = db.execute("SELECT status FROM submissions WHERE id=?", (submission["id"],)).fetchone()
        attempt = db.execute(
            "SELECT status,completed_at,report_json FROM submission_review_attempts WHERE submission_id=?",
            (submission["id"],),
        ).fetchone()
    assert current["status"] == "ai_failed"
    assert attempt["status"] == "ai_failed" and attempt["completed_at"]
    assert "orphan_attempt_recovered" in attempt["report_json"]


def test_workspace_suspend_after_claim_prevents_injected_review_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("dispatch-race@example.com", "Owner", "correct-horse-123")
    token, personal = platform.create_session(user["id"])
    team = platform.create_team(personal, "Dispatch Race Team")
    context = platform.switch_workspace(token, personal, team["id"])
    snapshot = {"title": "Dispatch race", "markdown": "# Dispatch race\n\nBody"}
    preview = platform.create_preview(
        context, "private/dispatch-race.md", "rev-dispatch-race", "6" * 32,
        snapshot_fingerprint(snapshot), snapshot,
    )
    submission = platform.submit_preview(context, preview["preview_id"])
    claimed, release = threading.Event(), threading.Event()
    original_claim = platform.claim_ai_submission

    def blocked_after_claim():
        row = original_claim()
        if row is not None:
            claimed.set()
            assert release.wait(3)
        return row

    calls: list[bool] = []
    monkeypatch.setattr(platform, "claim_ai_submission", blocked_after_claim)
    worker = PlatformReviewWorker(platform, reviewer=lambda _payload: calls.append(True) or {"decision": "pass"})
    try:
        assert claimed.wait(2)
        platform.change_workspace_lifecycle(context, team["id"], "suspend")
        release.set()
        deadline = time.time() + 2
        status = None
        while time.time() < deadline:
            with platform.connect() as db:
                status = db.execute("SELECT status FROM submissions WHERE id=?", (submission["id"],)).fetchone()[0]
            if status == "ai_failed":
                break
            time.sleep(0.02)
        assert not calls
        assert status == "ai_failed"
    finally:
        release.set()
        worker.close()


def test_workspace_suspend_waits_for_review_dispatch_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("dispatch@example.com", "Owner", "correct-horse-123")
    token, personal = platform.create_session(user["id"])
    team = platform.create_team(personal, "Dispatch Team")
    team_context = platform.switch_workspace(token, personal, team["id"])
    platform.save_model(team["id"], "openai-compatible", "https://team.example/v1", "key", "model")
    snapshot = {"title": "Dispatch", "markdown": "# Dispatch\n\nBody"}
    preview = platform.create_preview(
        team_context, "private/dispatch.md", "rev-dispatch", "3" * 32,
        snapshot_fingerprint(snapshot), snapshot,
    )
    submission = platform.submit_preview(team_context, preview["preview_id"])
    entered, release, suspended = threading.Event(), threading.Event(), threading.Event()

    def blocked_review(*_args):
        entered.set()
        assert release.wait(3)
        return {"decision": "pass", "summary": "late"}

    monkeypatch.setattr(platform_review, "default_reviewer", blocked_review)
    worker = PlatformReviewWorker(platform)
    lifecycle = None
    try:
        assert entered.wait(2)

        def suspend():
            platform.change_workspace_lifecycle(team_context, team["id"], "suspend")
            suspended.set()

        lifecycle = threading.Thread(target=suspend)
        lifecycle.start()
        time.sleep(0.1)
        assert not suspended.is_set()
        release.set()
        lifecycle.join(3)
        assert suspended.is_set()
        with platform.connect() as db:
            status = db.execute("SELECT status FROM submissions WHERE id=?", (submission["id"],)).fetchone()[0]
        assert status == "ai_failed"
    finally:
        release.set()
        if lifecycle is not None:
            lifecycle.join(3)
        worker.close()


@pytest.mark.parametrize("content", [
    '```json\n{"decision":"pass","summary":"ok","issues":[]}\n```',
    '<think>internal review</think>\n{"decision":"needs_revision","summary":"fix it","issues":[]}',
    'Review result:\n{"decision":"reject","summary":"unsafe","issues":[]}\nDone.',
])
def test_platform_review_accepts_compatible_model_json_wrappers(content: str):
    result = parse_review_result(content)
    assert result["decision"] in {"pass", "needs_revision", "reject"}
    assert result["policy_version"] == platform_review.REVIEW_POLICY_VERSION


def test_platform_review_invalid_response_reports_actionable_code():
    result = review_failure(ValueError("invalid review response"))
    assert result["decision"] == "failed"
    assert result["issues"][0]["code"] == "invalid_response"


def test_login_rate_limit_account_delete_and_workspace_removal(multi_server):
    app, base = multi_server
    alice = Client(base)
    alice.register("alice@example.com", "Alice")
    context = context_for(app, alice)
    workspace_root = app.platform.workspace_root(context.workspace_root_name)
    attacker = Client(base)
    for index in range(5):
        assert attacker.request("POST", "/api/auth/login", {"email": "alice@example.com", "password": "wrong-password-123"}, key=f"bad-{index}")[0] == 401
    assert attacker.request("POST", "/api/auth/login", {"email": "alice@example.com", "password": "wrong-password-123"}, key="blocked")[0] == 429
    status, payload = alice.request("POST", "/api/account/delete", {"password": "correct-horse-123"}, key="delete-account")
    assert status == 200 and payload["deleted"] is True
    assert not workspace_root.exists()
    assert alice.request("GET", "/api/articles")[0] == 401
