import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from platform_store import PlatformStore
from publication import snapshot_fingerprint
from serve import create_app
from square_v2 import PublicIndexWorker, REUSE_POLICY_VERSION, canonical_public_url, safe_public_url
from wiki_service import WikiService


def _snapshot(title: str, body: str, *, category_id: str | None = None, tag_ids: list[str] | None = None,
              permission: str = "view_only") -> dict:
    return {
        "title": title,
        "category": "private-research",
        "content_status": "词条",
        "markdown": f"# {title}\n\n{body}\n",
        "summary": f"{title} summary",
        "attribution": "Square Author",
        "source_summaries": ["private/raw/path.md", "file:///private/source"],
        "public_sources": [{"label": "Official reference", "url": "https://example.com/reference", "kind": "reference"}],
        "square": {
            "public_category_id": category_id,
            "tag_ids": tag_ids or [],
            "reuse_permission": permission,
            "reuse_policy_version": REUSE_POLICY_VERSION,
            "reuse_policy_acknowledged": permission == "allow_private_copy",
            "link_public_profile": False,
        },
    }


def _publish(store: PlatformStore, context, snapshot: dict, *, article_id: str, source_revision: str,
             category_id: str | None = None, tag_ids: list[str] | None = None) -> dict:
    preview = store.create_preview(
        context, f"concepts/{article_id}.md", source_revision, article_id,
        snapshot_fingerprint(snapshot), snapshot,
    )
    submission = store.submit_preview(context, preview["preview_id"])
    store.ai_decide(submission["id"], "pass", {"summary": "platform approved", "issues": []})
    return store.admin_decide(
        context, submission["id"], "approve", "Admin reviewed",
        public_category_id=category_id, tag_ids=tag_ids,
    )


@pytest.fixture
def square(tmp_path):
    store = PlatformStore(tmp_path)
    user, _ = store.register("square-owner@example.com", "Square Owner", "correct-horse-123")
    _token, context = store.create_session(user["id"])
    category = store.admin_upsert_category(context, None, "knowledge-systems", "知识系统", "公共分类", "active", 0)
    tag = store.admin_upsert_tag(context, None, "retrieval", "检索", "active")
    return store, context, category, tag


def test_square_search_uses_public_projection_cursor_and_chinese(square):
    store, context, category, tag = square
    first = _publish(
        store, context, _snapshot("知识图谱检索", "正文包含向量数据库。", category_id=category["id"], tag_ids=[tag["id"]]),
        article_id="a" * 32, source_revision="r1", category_id=category["id"], tag_ids=[tag["id"]],
    )
    second = _publish(
        store, context, _snapshot("第二篇知识文章", "公开正文。", category_id=category["id"]),
        article_id="b" * 32, source_revision="r2", category_id=category["id"],
    )

    page = store.search_public(query="知识图谱", category="knowledge-systems", sort="relevance", limit=1)
    assert [item["id"] for item in page["items"]] == [first["public_entry_id"]]
    assert page["next_cursor"] is None
    first_page = store.search_public(query="知识", sort="relevance", limit=1)
    assert first_page["next_cursor"]
    # A changed FTS corpus expires old BM25 positions instead of repeating rows.
    store.remove_public(context, first_page["items"][0]["id"], "pagination race")
    with pytest.raises(ValueError, match="cursor does not match"):
        store.search_public(query="知识", sort="relevance", limit=1, cursor=first_page["next_cursor"])
    with pytest.raises(ValueError, match="cursor does not match"):
        store.search_public(query="不同查询", sort="relevance", limit=1, cursor=first_page["next_cursor"])


def test_search_index_failure_does_not_rollback_public_fact_and_is_retryable(square, monkeypatch):
    store, context, category, _tag = square
    snapshot = _snapshot("Index retry", "The public fact must survive.", category_id=category["id"])
    preview = store.create_preview(
        context, "concepts/index-retry.md", "r1", "f" * 32,
        snapshot_fingerprint(snapshot), snapshot,
    )
    submission = store.submit_preview(context, preview["preview_id"])
    store.ai_decide(submission["id"], "pass", {"summary": "pass", "issues": []})
    original = store._sync_square_entry
    monkeypatch.setattr(
        store, "_sync_square_entry",
        lambda _db, _entry_id: (_ for _ in ()).throw(sqlite3.OperationalError("fts unavailable")),
    )

    approved = store.admin_decide(
        context, submission["id"], "approve", "Admin reviewed",
        public_category_id=category["id"],
    )
    entry_id = approved["public_entry_id"]
    assert store.get_public_v2(entry_id)["snapshot"]["title"] == "Index retry"
    with store.connect() as db:
        job = db.execute("SELECT status,attempts FROM public_index_jobs WHERE entry_id=?", (entry_id,)).fetchone()
    assert dict(job) == {"status": "pending", "attempts": 1}

    monkeypatch.setattr(store, "_sync_square_entry", original)
    assert store.rebuild_public_search() == {"indexed": 1, "pending": 0}
    assert store.search_public(query="Index retry")["items"][0]["id"] == entry_id

    monkeypatch.setattr(
        store, "_sync_square_entry",
        lambda _db, _entry_id: (_ for _ in ()).throw(sqlite3.OperationalError("fts unavailable")),
    )
    store.remove_public(context, entry_id, "policy review")
    assert store.search_public(query="Index retry")["items"] == []


def test_public_index_worker_retries_pending_projection_without_restart(square, monkeypatch):
    store, context, category, _tag = square
    snapshot = _snapshot("Worker retry", "Search projection", category_id=category["id"])
    preview = store.create_preview(
        context, "concepts/worker-retry.md", "r1", "0" * 32,
        snapshot_fingerprint(snapshot), snapshot,
    )
    submission = store.submit_preview(context, preview["preview_id"])
    store.ai_decide(submission["id"], "pass", {"summary": "pass", "issues": []})
    original = store._sync_square_entry
    monkeypatch.setattr(
        store, "_sync_square_entry",
        lambda _db, _entry_id: (_ for _ in ()).throw(sqlite3.OperationalError("temporary fts failure")),
    )
    approved = store.admin_decide(
        context, submission["id"], "approve", "Admin reviewed",
        public_category_id=category["id"],
    )
    monkeypatch.setattr(store, "_sync_square_entry", original)

    worker = PublicIndexWorker(store)
    try:
        worker.wake()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with store.connect() as db:
                if db.execute(
                    "SELECT 1 FROM public_index_jobs WHERE entry_id=?", (approved["public_entry_id"],),
                ).fetchone() is None:
                    break
            time.sleep(0.02)
        else:
            pytest.fail("public index worker did not reconcile the pending job")
    finally:
        worker.close()
    assert store.search_public(query="Worker retry")["items"][0]["id"] == approved["public_entry_id"]


def test_public_index_worker_can_run_when_remote_workers_are_disabled(tmp_path):
    app = create_app(
        tmp_path, tmp_path / "viewer", multi_user=True,
        start_worker=False, start_public_index_worker=True,
    )
    try:
        assert app.review_worker is None
        assert app.public_index_worker is not None
        assert app.public_index_worker._thread.is_alive()
    finally:
        app.close()


def test_public_index_worker_dead_letters_dirty_job_and_keeps_running(square):
    store, context, category, _tag = square
    approved = _publish(
        store, context, _snapshot("Dirty index job", "Runtime corruption", category_id=category["id"]),
        article_id="9" * 32, source_revision="dirty-job", category_id=category["id"],
    )
    entry_id = approved["public_entry_id"]
    with store.connect() as db:
        db.execute("""
            INSERT INTO public_index_jobs(entry_id,status,attempts,last_error,not_before,updated_at)
            VALUES(?,'pending','not-an-integer',NULL,NULL,'2026-01-01T00:00:00+00:00')
        """, (entry_id,))

    worker = PublicIndexWorker(store)
    try:
        worker.wake()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with store.connect() as db:
                job = db.execute(
                    "SELECT status,last_error FROM public_index_jobs WHERE entry_id=?", (entry_id,),
                ).fetchone()
            if job is not None and job["status"] == "dead":
                break
            time.sleep(0.02)
        else:
            pytest.fail("dirty public index job was not dead-lettered")
        assert job["last_error"] == "invalid index job state"
        assert worker._thread.is_alive()
    finally:
        worker.close()


def test_public_index_claim_compares_non_utc_schedule_by_instant(square):
    store, context, category, _tag = square
    approved = _publish(
        store, context, _snapshot("Offset schedule", "Timezone semantics", category_id=category["id"]),
        article_id="8" * 32, source_revision="offset-job", category_id=category["id"],
    )
    entry_id = approved["public_entry_id"]
    with store.connect() as db:
        db.execute("""
            INSERT INTO public_index_jobs(entry_id,status,attempts,last_error,not_before,updated_at)
            VALUES(?,'retry',1,NULL,'2026-01-01T10:00:00+08:00','2026-01-01T00:00:00+00:00')
        """, (entry_id,))

    assert store.claim_public_index_job() == {"entry_id": entry_id, "attempt": 2}


def test_public_index_claim_normalizes_future_offset_schedule_without_running_it(square):
    store, context, category, _tag = square
    approved = _publish(
        store, context, _snapshot("Future schedule", "Timezone normalization", category_id=category["id"]),
        article_id="3" * 32, source_revision="future-offset", category_id=category["id"],
    )
    entry_id = approved["public_entry_id"]
    future = datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)
    with store.connect() as db:
        db.execute("""
            INSERT INTO public_index_jobs(entry_id,status,attempts,last_error,not_before,updated_at)
            VALUES(?,'retry',1,NULL,?,'2026-01-01T08:00:00+08:00')
        """, (entry_id, future.isoformat(timespec="seconds")))

    assert store.claim_public_index_job() is None
    with store.connect() as db:
        job = db.execute(
            "SELECT status,not_before,updated_at FROM public_index_jobs WHERE entry_id=?", (entry_id,),
        ).fetchone()
    assert job["status"] == "retry"
    assert job["not_before"].endswith("+00:00")
    assert job["updated_at"] == "2026-01-01T00:00:00+00:00"


def test_public_index_claim_recovers_offset_running_lease_and_dead_letters_bad_lease(square):
    store, context, category, _tag = square
    stale = _publish(
        store, context, _snapshot("Stale lease", "Offset timestamp", category_id=category["id"]),
        article_id="7" * 32, source_revision="stale-lease", category_id=category["id"],
    )["public_entry_id"]
    malformed = _publish(
        store, context, _snapshot("Bad lease", "Malformed timestamp", category_id=category["id"]),
        article_id="6" * 32, source_revision="bad-lease", category_id=category["id"],
    )["public_entry_id"]
    stale_at = datetime.now(timezone(timedelta(hours=8))) - timedelta(minutes=6)
    with store.connect() as db:
        db.executemany("""
            INSERT INTO public_index_jobs(entry_id,status,attempts,last_error,not_before,updated_at)
            VALUES(?,'running',1,NULL,NULL,?)
        """, [(stale, stale_at.isoformat(timespec="seconds")), (malformed, "not-a-date")])

    assert store.claim_public_index_job() == {"entry_id": stale, "attempt": 2}
    with store.connect() as db:
        bad = db.execute(
            "SELECT status,last_error FROM public_index_jobs WHERE entry_id=?", (malformed,),
        ).fetchone()
    assert dict(bad) == {"status": "dead", "last_error": "invalid index job state"}


def test_public_index_worker_survives_projection_runtime_error_and_processes_next_job(square, monkeypatch):
    store, context, category, _tag = square
    failed = _publish(
        store, context, _snapshot("Runtime failure", "First job", category_id=category["id"]),
        article_id="5" * 32, source_revision="runtime-failure", category_id=category["id"],
    )["public_entry_id"]
    following = _publish(
        store, context, _snapshot("Following job", "Second job", category_id=category["id"]),
        article_id="4" * 32, source_revision="following-job", category_id=category["id"],
    )["public_entry_id"]
    now = "2026-01-01T00:00:00+00:00"
    with store.connect() as db:
        db.executemany("""
            INSERT INTO public_index_jobs(entry_id,status,attempts,last_error,not_before,updated_at)
            VALUES(?,'pending',0,NULL,NULL,?)
        """, [(failed, now), (following, now)])
    original = store._sync_square_entry

    def fail_one(db, entry_id):
        if entry_id == failed:
            raise RuntimeError("unexpected projection failure")
        return original(db, entry_id)

    monkeypatch.setattr(store, "_sync_square_entry", fail_one)
    worker = PublicIndexWorker(store)
    try:
        worker.wake()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with store.connect() as db:
                first = db.execute(
                    "SELECT status,last_error FROM public_index_jobs WHERE entry_id=?", (failed,),
                ).fetchone()
                second = db.execute(
                    "SELECT 1 FROM public_index_jobs WHERE entry_id=?", (following,),
                ).fetchone()
            if first is not None and first["status"] == "retry" and second is None:
                break
            time.sleep(0.02)
        else:
            pytest.fail("worker did not isolate the failed job and process the next job")
        assert "unexpected projection failure" in first["last_error"]
        assert worker._thread.is_alive()
    finally:
        worker.close()


def test_public_dto_does_not_leak_private_snapshot_fields_and_snapshot_is_immutable(square):
    store, context, category, tag = square
    snapshot = _snapshot(
        "Public projection", "Visible body", category_id=category["id"], tag_ids=[tag["id"]],
        permission="allow_private_copy",
    )
    snapshot["markdown"] = """# Public projection

> Article-ID: cccccccccccccccccccccccccccccccc
> Category-ID: dddddddddddddddddddddddddddddddd
> Classification: confirmed
> Category: private-research
> Generation: local+llm; task=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee; state=succeeded
> Sources: private/raw/path.md

Visible body

正文中的分类：这句话必须保留。

---

*分类：_inbox*
*状态：草稿*
"""
    approved = _publish(
        store, context, snapshot, article_id="c" * 32, source_revision="r1",
        category_id=category["id"], tag_ids=[tag["id"]],
    )
    entry_id = approved["public_entry_id"]
    with store.connect() as db:
        before = db.execute("SELECT snapshot_json,content_hash FROM public_revisions WHERE entry_id=?", (entry_id,)).fetchone()

    public = store.get_public_v2(entry_id)
    assert public["sources"] == [{"label": "example.com", "url": "https://example.com/reference", "kind": "reference"}]
    assert public["source_count"] == 1
    assert public["correction_count"] == 0
    assert public["snapshot"]["source_summaries"] == []
    assert public["snapshot"]["category"] == ""
    assert public["category"]["name"] == category["name"]
    assert "Article-ID" not in public["snapshot"]["markdown"]
    assert "Category-ID" not in public["snapshot"]["markdown"]
    assert "Generation" not in public["snapshot"]["markdown"]
    assert "正文中的分类：这句话必须保留。" in public["snapshot"]["markdown"]
    assert "_inbox" not in public["snapshot"]["markdown"]
    assert "状态：草稿" not in public["snapshot"]["markdown"]
    assert "private-research" not in json.dumps(public, ensure_ascii=False)
    assert "square" not in public["snapshot"]
    assert "private/raw/path.md" not in json.dumps(public, ensure_ascii=False)
    store.admin_set_featured(context, entry_id, True, "Useful overview", 3)
    store.admin_set_entry_taxonomy(context, entry_id, category["id"], [tag["id"]])
    with store.connect() as db:
        after = db.execute("SELECT snapshot_json,content_hash FROM public_revisions WHERE entry_id=?", (entry_id,)).fetchone()
    assert tuple(after) == tuple(before)
    assert store.public_home()["featured"][0]["id"] == entry_id
    assert store.search_public(query="projection")["items"][0]["source_count"] == 1


def test_legacy_public_preamble_does_not_expose_raw_paths(square):
    store, context, category, _tag = square
    snapshot = _snapshot("Legacy projection", "Visible body", category_id=category["id"])
    snapshot["markdown"] = """# Legacy projection

> Category: tools
> Status: 词条

> Evidence: internal research
> Raw: ../../raw/private/source.md

Visible body

> Raw: this body quote is public prose
"""
    approved = _publish(
        store, context, snapshot, article_id="9" * 32, source_revision="r1",
        category_id=category["id"],
    )
    with store.connect() as db:
        before = db.execute(
            "SELECT snapshot_json,content_hash FROM public_revisions WHERE entry_id=?",
            (approved["public_entry_id"],),
        ).fetchone()
    public = store.get_public_v2(approved["public_entry_id"])
    markdown = public["snapshot"]["markdown"]
    assert "../../raw/private/source.md" not in markdown
    assert "internal research" not in markdown
    assert "Visible body" in markdown
    assert "this body quote is public prose" in markdown
    assert store.search_public(query="private source")["items"] == []
    with store.connect() as db:
        after = db.execute(
            "SELECT snapshot_json,content_hash FROM public_revisions WHERE entry_id=?",
            (approved["public_entry_id"],),
        ).fetchone()
    assert tuple(after) == tuple(before)


def test_public_sources_require_explicit_global_http_urls(square):
    store, context, category, _tag = square
    snapshot = _snapshot("Safe sources", "Body", category_id=category["id"])
    snapshot["source_summaries"] = ["https://legacy.example/private-selection"]
    snapshot["public_sources"] = [
        {"label": "example.com", "url": "https://example.com/reference", "kind": "reference"},
        {"label": "Loopback", "url": "http://127.0.0.1/private", "kind": "reference"},
        {"label": "Private network", "url": "https://10.0.0.8/source", "kind": "reference"},
        {"label": "Local name", "url": "https://wiki.internal/source", "kind": "reference"},
        {"label": "Credentials", "url": "https://user:pass@example.com/source", "kind": "reference"},
    ]
    approved = _publish(store, context, snapshot, article_id="9" * 32, source_revision="r1",
                        category_id=category["id"])
    assert store.get_public_v2(approved["public_entry_id"])["sources"] == [
        {"label": "example.com", "url": "https://example.com/reference", "kind": "reference"},
    ]
    assert safe_public_url("https://example.com/reference") is True
    assert safe_public_url("https://127.0.0.1/private") is False
    assert safe_public_url("https://wiki.internal/private") is False
    assert canonical_public_url("https://[2606:4700:4700::1111]/dns") == "https://[2606:4700:4700::1111]/dns"


def test_private_copy_preview_requires_current_policy_acknowledgement(square):
    store, context, category, _tag = square
    snapshot = _snapshot(
        "Consent required", "Body", category_id=category["id"], permission="allow_private_copy",
    )
    snapshot["square"]["reuse_policy_acknowledged"] = False
    with pytest.raises(ValueError, match="acknowledged"):
        store.create_preview(
            context, "concepts/consent.md", "r1", "0" * 32,
            snapshot_fingerprint(snapshot), snapshot,
        )
    snapshot["square"]["reuse_policy_acknowledged"] = True
    snapshot["square"]["reuse_policy_version"] = "obsolete-policy"
    with pytest.raises(ValueError, match="acknowledged"):
        store.create_preview(
            context, "concepts/consent.md", "r1", "0" * 32,
            snapshot_fingerprint(snapshot), snapshot,
        )


def test_review_attempt_history_is_immutable_and_stale_results_are_fenced(square):
    store, context, category, _tag = square
    snapshot = _snapshot("Review history", "Body", category_id=category["id"])
    preview = store.create_preview(context, "concepts/review.md", "r1", "8" * 32,
                                   snapshot_fingerprint(snapshot), snapshot)
    submission = store.submit_preview(context, preview["preview_id"])
    claimed = store.claim_ai_submission()
    assert claimed and claimed["id"] == submission["id"] and claimed["attempt"] == 1
    result = {
        "summary": "Unavailable", "issues": [], "policy_version": "policy-v1",
        "provider": "platform", "model": "reviewer-v1", "rules_version": "rules-v1",
    }
    store.ai_decide(submission["id"], "failed", result, expected_attempt=1)
    store.retry_ai(context, submission["id"])
    claimed_again = store.claim_ai_submission()
    assert claimed_again and claimed_again["attempt"] == 2
    store.ai_decide(submission["id"], "pass", {**result, "model": "reviewer-v2"}, expected_attempt=2)
    stale = store.ai_decide(submission["id"], "reject", result, expected_attempt=1)
    assert stale["stale"] is True
    with store.connect() as db:
        attempts = db.execute("""
            SELECT attempt,status,model,report_json FROM submission_review_attempts
            WHERE submission_id=? ORDER BY attempt
        """, (submission["id"],)).fetchall()
    assert [(row["attempt"], row["status"], row["model"]) for row in attempts] == [
        (1, "ai_failed", "reviewer-v1"), (2, "pending_admin", "reviewer-v2"),
    ]
    assert json.loads(attempts[0]["report_json"])["decision"] == "failed"


@pytest.mark.parametrize(
    ("duplicate_action", "expected"),
    [("request_changes", "admin_changes_requested"), ("reject_duplicate", "admin_rejected")],
)
def test_duplicate_review_action_changes_the_submission_decision(square, duplicate_action, expected):
    store, context, category, _tag = square
    snapshot = _snapshot(f"Duplicate {duplicate_action}", "Body", category_id=category["id"])
    preview = store.create_preview(context, f"concepts/{duplicate_action}.md", "r1", "7" * 32,
                                   snapshot_fingerprint(snapshot), snapshot)
    submission = store.submit_preview(context, preview["preview_id"])
    store.ai_decide(submission["id"], "pass", {"summary": "pass", "issues": []})
    result = store.admin_decide(
        context, submission["id"], "approve", "Duplicate review", duplicate_action=duplicate_action,
    )
    assert result["status"] == expected
    assert result["public_entry_id"] is None


def test_cancel_withdraw_takedown_and_revision_isolation_are_distinct(square):
    store, context, category, _tag = square
    pending_snapshot = _snapshot("Pending", "Draft", category_id=category["id"])
    preview = store.create_preview(context, "concepts/pending.md", "pending-r1", "d" * 32,
                                   snapshot_fingerprint(pending_snapshot), pending_snapshot)
    submission = store.submit_preview(context, preview["preview_id"])
    assert store.withdraw(context, submission["id"])["status"] == "withdrawn"
    assert store.search_public(query="Pending")["items"] == []

    v1 = _publish(store, context, _snapshot("Versioned", "Version one", category_id=category["id"]),
                  article_id="e" * 32, source_revision="v1", category_id=category["id"])
    v2 = _publish(store, context, _snapshot("Versioned", "Version two", category_id=category["id"]),
                  article_id="e" * 32, source_revision="v2", category_id=category["id"])
    entry_id = v1["public_entry_id"]
    assert v2["public_entry_id"] == entry_id
    versions = store.public_versions(entry_id)
    assert "Version one" in store.public_diff(entry_id, 1, 2)["diff"]
    with pytest.raises(ValueError, match="adjacent"):
        store.public_diff(entry_id, 1, 1)
    old_revision = next(item for item in versions if item["version"] == 1)
    store.admin_isolate_revision(context, entry_id, old_revision["id"], True, "privacy")
    admin_history = store.admin_public_versions(context, entry_id)
    assert next(item for item in admin_history if item["version"] == 1)["visibility"] == "isolated"
    with pytest.raises(FileNotFoundError):
        store.get_public_version(entry_id, 1)
    with pytest.raises(ValueError, match="take down"):
        store.admin_isolate_revision(context, entry_id, versions[0]["id"], True, "privacy")
    store.remove_public(context, entry_id, "moderation")
    with pytest.raises(FileNotFoundError):
        store.get_public_v2(entry_id)
    store.relist_public(context, entry_id, "resolved")
    assert store.get_public_v2(entry_id)["version"] == 2
    store.author_withdraw_public(context, entry_id, "author decision")
    with pytest.raises(FileNotFoundError):
        store.get_public_v2(entry_id)
    with pytest.raises(FileNotFoundError):
        store.relist_public(context, entry_id, "Admin cannot override author")


def test_import_is_idempotent_and_keeps_private_identity_and_provenance(square, tmp_path):
    store, context, category, _tag = square
    approved = _publish(
        store, context,
        _snapshot("Reusable", "Public body", category_id=category["id"], permission="allow_private_copy"),
        article_id="f" * 32, source_revision="r1", category_id=category["id"],
    )
    entry_id = approved["public_entry_id"]
    revision_id = store.get_public_v2(entry_id)["revision_id"]
    first = store.begin_public_import(
        context, entry_id, revision_id,
        expected_workspace_id=context.workspace_id,
        expected_policy_version=REUSE_POLICY_VERSION, acknowledged=True,
    )
    replay = store.begin_public_import(
        context, entry_id, revision_id,
        expected_workspace_id=context.workspace_id,
        expected_policy_version=REUSE_POLICY_VERSION, acknowledged=True,
    )
    assert replay["id"] == first["id"] and replay["replay"] is True
    assert first["private_article_id"] != "f" * 32
    service = WikiService(store.workspace_root(context.workspace_root_name), start_worker=False)
    try:
        imported = service.import_public_article(first)
        store.finish_public_import(context, first["id"])
        article = service.read_article(imported["article"]["path"])
    finally:
        service.close()
    assert article["article_id"] == first["private_article_id"]
    assert f"> Public-Entry: {entry_id}" in article["markdown"]
    assert f"> Public-Revision: {revision_id}" in article["markdown"]
    assert f"> Public-Reuse-Policy: {REUSE_POLICY_VERSION}" in article["markdown"]
    with store.connect() as db:
        saved_import = db.execute("SELECT policy_version FROM public_imports WHERE id=?", (first["id"],)).fetchone()
    assert saved_import["policy_version"] == REUSE_POLICY_VERSION
    assert store.get_public_v2(entry_id, context.user_id, context.workspace_id)["imported"] is True
    assert store.get_public_v2(entry_id, context.user_id, "0" * 32)["imported"] is False
    store.set_reuse_permission(context, entry_id, "view_only")
    existing = store.begin_public_import(
        context, entry_id, revision_id,
        expected_workspace_id=context.workspace_id,
        expected_policy_version=REUSE_POLICY_VERSION, acknowledged=True,
    )
    assert existing["id"] == first["id"] and existing["existing"] is True
    assert article["path"].startswith("_inbox/")
    assert store.my_square_library(context)["subscriptions"][0]["entry_id"] == entry_id


def test_subscription_correction_profile_and_collection_flow(square):
    store, context, category, _tag = square
    approved = _publish(store, context, _snapshot("Interactive", "Body", category_id=category["id"]),
                        article_id="1" * 32, source_revision="r1", category_id=category["id"])
    entry_id = approved["public_entry_id"]
    assert store.set_subscription(context, entry_id, True)["subscribed"] is True
    correction = store.create_correction(context, entry_id, "factual", "Please verify this statement", "HTTP://EXAMPLE.COM/evidence")
    author_correction = store.list_entry_corrections(context, entry_id)[0]
    assert author_correction["id"] == correction["id"]
    assert "submitter_id" not in author_correction
    assert author_correction["evidence_url"] == "http://example.com/evidence"
    assert "submitter_id" not in store.list_my_corrections(context)[0]
    assert store.admin_square_state(context)["corrections"][0]["id"] == correction["id"]
    decided = store.decide_correction(context, correction["id"], "accepted", "Will address in the next public revision")
    assert decided["status"] == "accepted"
    profile = store.set_public_profile(context, True, "Public Author", "Public bio")
    with store.connect() as db:
        db.execute("UPDATE public_entries SET public_profile_id=? WHERE id=?", (profile["id"], entry_id))
    assert store.get_public_profile(profile["id"])["entries"][0]["id"] == entry_id
    collection = store.admin_upsert_collection(
        context, None, "recommended-reading", "Recommended reading", "Curated", "published",
        [{"entry_id": entry_id, "note": "Start here"}], "Editorial selection",
    )
    assert store.get_public_collection(collection["slug"])["items"][0]["curator_note"] == "Start here"
    library = store.my_square_library(context)
    assert library["subscriptions"][0]["entry_id"] == entry_id
    assert library["profile"]["status"] == "active"
    store.set_public_profile(context, False, "Public Author", "Public bio")
    assert store.public_profile_tombstone(profile["id"])["code"] == "public_profile_disabled"


def test_report_takedown_notifies_subscribers_and_uses_reported_revision(square):
    store, admin, category, _tag = square
    published = _publish(store, admin, _snapshot("Reported", "Version one", category_id=category["id"]),
                         article_id="4" * 32, source_revision="r1", category_id=category["id"])
    entry_id = published["public_entry_id"]
    reader, _ = store.register("square-reader@example.com", "Reader", "correct-horse-123")
    _reader_token, reader_context = store.create_session(reader["id"])
    store.set_subscription(reader_context, entry_id, True)
    report = store.report_public(entry_id, reader["id"], "privacy", "Contains personal data")
    store.decide_report(admin, report["id"], "remove", "Privacy review confirmed")
    notifications = store.list_notifications(reader_context)
    assert any(item["kind"] == "public_removed" and item["object_id"] == entry_id for item in notifications)
    assert any(item["kind"] == "report_resolved" and item["object_id"] == report["id"] for item in notifications)


def test_account_deletion_disables_public_profile_and_future_reuse(square):
    store, context, category, _tag = square
    published = _publish(
        store, context,
        _snapshot("Retained attribution", "Body", category_id=category["id"], permission="allow_private_copy"),
        article_id="5" * 32, source_revision="r1", category_id=category["id"],
    )
    entry_id = published["public_entry_id"]
    reader, _ = store.register("deletion-subscriber@example.com", "Subscriber", "correct-horse-123")
    _reader_token, reader_context = store.create_session(reader["id"])
    store.set_subscription(reader_context, entry_id, True)
    profile = store.set_public_profile(context, True, "Public Author", "Biography")
    with store.connect() as db:
        db.execute("UPDATE public_entries SET public_profile_id=? WHERE id=?", (profile["id"], entry_id))
    store.delete_account(context, "correct-horse-123")
    with store.connect() as db:
        entry = db.execute("SELECT status FROM public_entries WHERE id=?", (entry_id,)).fetchone()
        profile_row = db.execute("SELECT status FROM public_profiles WHERE id=?", (profile["id"],)).fetchone()
        permission = db.execute("SELECT permission,revoked_at FROM public_reuse_permissions WHERE entry_id=?", (entry_id,)).fetchone()
        assert db.execute("SELECT 1 FROM public_search_documents WHERE entry_id=?", (entry_id,)).fetchone() is None
        assert db.execute("SELECT 1 FROM public_search_fts WHERE entry_id=?", (entry_id,)).fetchone() is None
    assert entry["status"] == "withdrawn_by_author"
    assert profile_row["status"] == "disabled"
    assert permission["permission"] == "view_only" and permission["revoked_at"]
    assert store.public_entry_tombstone(entry_id)["code"] == "public_entry_account_deleted"
    notifications = store.list_notifications(reader_context)
    withdrawal = next(item for item in notifications if item["kind"] == "public_withdrawn")
    assert withdrawal["object_id"] == entry_id
    assert "注销" not in withdrawal["title"] + withdrawal["message"]


def test_account_deletion_is_terminal_for_removed_entries_and_pending_submissions(square):
    store, owner, category, _tag = square
    other, _ = store.register("other-admin@example.com", "Other Admin", "correct-horse-123")
    with store.connect() as db:
        db.execute("UPDATE users SET role='admin' WHERE id=?", (other["id"],))
        db.commit()
    _token, other_admin = store.create_session(other["id"])

    published = _publish(
        store, owner, _snapshot("Terminal entry", "Body", category_id=category["id"]),
        article_id="6" * 32, source_revision="r1", category_id=category["id"],
    )
    entry_id = published["public_entry_id"]
    store.remove_public(other_admin, entry_id, "temporary moderation")
    pending_snapshot = _snapshot("Pending after delete", "Body", category_id=category["id"])
    preview = store.create_preview(
        owner, "concepts/pending-delete.md", "r1", "7" * 32,
        snapshot_fingerprint(pending_snapshot), pending_snapshot,
    )
    submission = store.submit_preview(owner, preview["preview_id"])
    store.ai_decide(submission["id"], "pass", {"summary": "pass", "issues": []})

    store.delete_account(owner, "correct-horse-123")
    with pytest.raises(FileNotFoundError):
        store.relist_public(other_admin, entry_id, "must not restore deleted author")
    with pytest.raises(RuntimeError, match="already been decided"):
        store.admin_decide(other_admin, submission["id"], "approve", "must not publish")
    with store.connect() as db:
        assert db.execute("SELECT status FROM public_entries WHERE id=?", (entry_id,)).fetchone()[0] == "withdrawn_by_author"
        assert db.execute("SELECT status FROM submissions WHERE id=?", (submission["id"],)).fetchone()[0] == "withdrawn"


def test_import_only_accepts_current_revision_and_later_approval_keeps_entry_permission(square):
    store, context, category, _tag = square
    first = _publish(
        store, context,
        _snapshot("Current only", "Version one", category_id=category["id"], permission="allow_private_copy"),
        article_id="8" * 32, source_revision="r1", category_id=category["id"],
    )
    first_revision = store.get_public_v2(first["public_entry_id"])["revision_id"]

    second_snapshot = _snapshot(
        "Current only", "Version two", category_id=category["id"], permission="allow_private_copy",
    )
    preview = store.create_preview(
        context, "concepts/current-only.md", "r2", "8" * 32,
        snapshot_fingerprint(second_snapshot), second_snapshot,
    )
    submission = store.submit_preview(context, preview["preview_id"])
    store.ai_decide(submission["id"], "pass", {"summary": "pass", "issues": []})
    store.set_reuse_permission(context, first["public_entry_id"], "view_only")
    store.admin_decide(context, submission["id"], "approve", "new revision", public_category_id=category["id"])
    current = store.get_public_v2(first["public_entry_id"])
    assert current["reuse_permission"] == "view_only"
    with pytest.raises(FileNotFoundError):
        store.begin_public_import(
            context, first["public_entry_id"], first_revision,
            expected_workspace_id=context.workspace_id,
            expected_policy_version=REUSE_POLICY_VERSION, acknowledged=True,
        )


@pytest.mark.parametrize("url", [
    "http://2130706433/x", "http://127.1/x", "http://0177.0.0.1/x",
    "http://0x7f000001/x", "https://[::ffff:127.0.0.1]/x",
    "http://0x7f.1/x", "http://0xc0.0250.1.1/x", "http://127.0x0.0.1/x",
    "\nhttps://example.com/x", "https://exa mple.com/x", "https://example.com:99999/x",
])
def test_public_url_rejects_noncanonical_or_non_global_hosts(url):
    assert safe_public_url(url) is False
    assert canonical_public_url(url) is None


def test_public_source_output_revalidates_legacy_rows(square):
    store, context, category, _tag = square
    published = _publish(
        store, context, _snapshot("Output fence", "Body", category_id=category["id"]),
        article_id="9" * 32, source_revision="r1", category_id=category["id"],
    )
    entry_id = published["public_entry_id"]
    revision_id = store.get_public_v2(entry_id)["revision_id"]
    with store.connect() as db:
        db.execute(
            "INSERT INTO public_revision_sources VALUES(?,?,?,?,?)",
            (revision_id, 99, "private/raw/path.md", "http://2130706433/private", "reference"),
        )
        db.commit()
    result = store.get_public_v2(entry_id)
    assert result["sources"] == [{
        "label": "example.com", "url": "https://example.com/reference", "kind": "reference",
    }]
    assert result["source_count"] == 1
    store.admin_set_featured(context, entry_id, True, "source count", 0)
    assert store.search_public(query="Output fence")["items"][0]["source_count"] == 1
    assert store.public_home()["featured"][0]["source_count"] == 1


def test_stale_account_context_cannot_write_square_state(square):
    store, context, category, _tag = square
    published = _publish(
        store, context, _snapshot("Revoked account", "Body", category_id=category["id"]),
        article_id="a" * 32, source_revision="r1", category_id=category["id"],
    )
    entry_id = published["public_entry_id"]
    with store.connect() as db:
        before = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("public_subscriptions", "correction_suggestions", "public_profiles", "reports")
        }
        db.execute("UPDATE users SET status='suspended' WHERE id=?", (context.user_id,))
        db.commit()

    actions = [
        lambda: store.set_subscription(context, entry_id, True),
        lambda: store.create_correction(context, entry_id, "factual", "Incorrect fact"),
        lambda: store.set_public_profile(context, True, "Stale", "Should not persist"),
        lambda: store.set_reuse_permission(
            context, entry_id, "allow_private_copy",
            policy_version=REUSE_POLICY_VERSION, acknowledged=True,
        ),
        lambda: store.report_public(entry_id, context.user_id, "other", "Should not persist"),
    ]
    for action in actions:
        with pytest.raises(FileNotFoundError):
            action()

    with store.connect() as db:
        after = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
    assert after == before


def test_category_slug_redirect_merge_mapping_and_revision_taxonomy(square):
    store, context, category, tag = square
    other = store.admin_upsert_category(context, None, "retrieval-systems", "检索系统", "Target", "active", 2)
    published = _publish(
        store, context, _snapshot("Taxonomy history", "Body", category_id=category["id"], tag_ids=[tag["id"]]),
        article_id="2" * 32, source_revision="r1", category_id=category["id"], tag_ids=[tag["id"]],
    )
    entry_id = published["public_entry_id"]
    renamed = store.admin_upsert_category(
        context, category["id"], "knowledge-infrastructure", "知识基础设施", "Renamed", "active", 1,
    )
    assert store.resolve_public_category("knowledge-systems")["slug"] == renamed["slug"]
    assert store.search_public(category="knowledge-systems")["items"][0]["id"] == entry_id
    merged = store.admin_merge_category(context, category["id"], other["id"], "Consolidate taxonomy")
    assert merged["canonical_slug"] == "retrieval-systems"
    assert store.resolve_public_category("knowledge-infrastructure")["id"] == other["id"]
    assert store.search_public(category="knowledge-systems")["items"][0]["category"]["id"] == other["id"]
    history = store.public_versions(entry_id)
    assert history[0]["category"]["id"] == category["id"]
    assert history[0]["tags"][0]["id"] == tag["id"]
    assert history[0]["attribution"] == "Square Author"

    unmapped = _publish(
        store, context, _snapshot("Needs mapping", "Body"), article_id="3" * 32, source_revision="r1",
    )
    assert store.get_public_v2(unmapped["public_entry_id"])["category"]["id"] is None
    result = store.admin_map_category(context, "private-research", other["id"])
    assert result["migrated"] == 1
    assert store.get_public_v2(unmapped["public_entry_id"])["category"]["id"] == other["id"]


def test_public_review_issues_are_allowlisted_without_model_text(square):
    store, context, category, _tag = square
    snapshot = _snapshot("Review projection", "Visible", category_id=category["id"])
    preview = store.create_preview(
        context, "concepts/review.md", "r1", "d" * 32,
        snapshot_fingerprint(snapshot), snapshot,
    )
    submission = store.submit_preview(context, preview["preview_id"])
    private_text = "private-research raw/secret.md"
    store.ai_decide(submission["id"], "pass", {
        "summary": "pass",
        "issues": [
            {"code": "privacy", "location": private_text, "explanation": private_text},
            {"code": "invented", "explanation": private_text},
        ],
    })
    approved = store.admin_decide(
        context, submission["id"], "approve", "Admin reviewed", public_category_id=category["id"],
    )
    public = store.get_public_v2(approved["public_entry_id"])
    assert public["review"]["issues"] == [{"code": "privacy"}]
    assert private_text not in json.dumps(public, ensure_ascii=False)


def test_import_workspace_precondition_and_recovered_operation_retry(square):
    store, context, category, _tag = square
    approved = _publish(
        store, context,
        _snapshot("Recoverable import", "Body", category_id=category["id"], permission="allow_private_copy"),
        article_id="e" * 32, source_revision="r1", category_id=category["id"],
    )
    detail = store.get_public_v2(approved["public_entry_id"])
    with pytest.raises(ValueError, match="workspace changed"):
        store.begin_public_import(
            context, approved["public_entry_id"], detail["revision_id"],
            expected_workspace_id="0" * 32,
            expected_policy_version=REUSE_POLICY_VERSION, acknowledged=True,
        )
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM public_imports").fetchone()[0] == 0

    intent = store.begin_public_import(
        context, approved["public_entry_id"], detail["revision_id"],
        expected_workspace_id=context.workspace_id,
        expected_policy_version=REUSE_POLICY_VERSION, acknowledged=True,
    )
    service = WikiService(store.workspace_root(context.workspace_root_name), start_worker=False)
    try:
        base = f"public-import-{intent['id']}"
        service.files.commit({"wiki/recovery-probe.md": "probe"}, kind="test", operation_id=base)
        service.files.rollback(base)
        imported = service.import_public_article(intent)
        assert imported["operation_id"] == f"{base}-attempt-2"
        assert imported["article"]["article_id"] == intent["private_article_id"]
        replayed = service.import_public_article(intent)
        assert replayed["operation_id"] == imported["operation_id"]
        assert replayed["replay"] is True
        store.finish_public_import(context, intent["id"])
        store.remap_public_import_paths(
            context.workspace_id, {intent["private_path"]: "concepts/moved-import.md"},
        )
        assert store.my_square_library(context)["imports"][0]["private_path"] == "concepts/moved-import.md"
    finally:
        service.close()


def test_stale_search_projection_is_hidden_from_profile_collection_and_related(square):
    store, context, category, _tag = square
    first = _publish(
        store, context, _snapshot("Stale title", "Body", category_id=category["id"]),
        article_id="1" * 32, source_revision="r1", category_id=category["id"],
    )
    second = _publish(
        store, context, _snapshot("Related peer", "Body", category_id=category["id"]),
        article_id="2" * 32, source_revision="r1", category_id=category["id"],
    )
    profile = store.set_public_profile(context, True, "Author", "Bio")
    collection = store.admin_upsert_collection(
        context, None, "stale-check", "Stale check", "", "published",
        [{"entry_id": first["public_entry_id"]}], "curated",
    )
    with store.connect() as db:
        db.execute("UPDATE public_entries SET public_profile_id=? WHERE id=?", (profile["id"], first["public_entry_id"]))
        new_revision = db.execute(
            "SELECT current_revision_id FROM public_entries WHERE id=?", (second["public_entry_id"],),
        ).fetchone()[0]
        db.execute("UPDATE public_entries SET current_revision_id=? WHERE id=?", (new_revision, first["public_entry_id"]))
        db.commit()
    assert store.get_public_profile(profile["id"])["entries"] == []
    assert store.get_public_collection(collection["slug"])["items"] == []
    assert all(item["id"] != first["public_entry_id"] for item in store.related_public(second["public_entry_id"]))


def test_taxonomy_refreshes_search_and_historical_slugs_are_reserved(square):
    store, context, category, tag = square
    published = _publish(
        store, context, _snapshot("Taxonomy projection", "Body", category_id=category["id"], tag_ids=[tag["id"]]),
        article_id="4" * 32, source_revision="r1", category_id=category["id"], tag_ids=[tag["id"]],
    )
    store.admin_upsert_category(context, category["id"], "knowledge-new", "独特新分类词", "", "active", 0)
    assert store.search_public(query="独特新分类词")["items"][0]["id"] == published["public_entry_id"]
    with pytest.raises(ValueError, match="reserved"):
        store.admin_upsert_category(context, None, "knowledge-systems", "劫持", "", "active", 0)
    assert store.resolve_public_category("knowledge-systems")["id"] == category["id"]
    store.admin_upsert_tag(context, tag["id"], "retrieval", "独特新标签词", "active")
    assert store.search_public(query="独特新标签词")["items"][0]["id"] == published["public_entry_id"]


def test_author_can_terminally_withdraw_admin_removed_entry(square):
    store, context, category, _tag = square
    published = _publish(
        store, context, _snapshot("Terminal withdraw", "Body", category_id=category["id"]),
        article_id="5" * 32, source_revision="r1", category_id=category["id"],
    )
    entry_id = published["public_entry_id"]
    store.remove_public(context, entry_id, "moderated")
    assert store.author_withdraw_public(context, entry_id, "author final decision")["status"] == "withdrawn_by_author"
    with pytest.raises(FileNotFoundError):
        store.relist_public(context, entry_id, "cannot restore")
