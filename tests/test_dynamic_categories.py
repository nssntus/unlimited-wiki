from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import uuid

import pytest

import dynamic_categories as dc
from wiki_service import WikiService


@pytest.fixture
def service(kb_root: Path):
    instance = WikiService(kb_root, start_worker=False)
    try:
        yield instance
    finally:
        instance.close()


def test_migration_adds_stable_ids_and_marks_legacy_uncategorized_pending(kb_root: Path):
    service = WikiService(kb_root, start_worker=False)
    try:
        categories = service.categories()
        assert {item["directory_name"] for item in categories} == {"concepts", "tools"}
        article = service.read_article("concepts/base.md")
        assert len(article["article_id"]) == 32
        assert article["primary_category_id"] is None
        assert article["classification_status"] == "pending"
        assert (kb_root / dc.REGISTRY_REL).is_file()
        stable_id = article["article_id"]
    finally:
        service.close()

    reopened = WikiService(kb_root, start_worker=False)
    try:
        assert reopened.read_article("concepts/base.md")["article_id"] == stable_id
    finally:
        reopened.close()


def test_generate_creates_pending_article_in_inbox(service: WikiService):
    result = service.generate("Dynamic Topic")
    assert result["created"] is True
    assert result["article"]["path"] == "_inbox/Dynamic-Topic.md"
    assert result["article"]["primary_category_id"] is None
    assert result["article"]["classification_status"] == "pending"


def test_preview_commit_inline_category_and_rollback(service: WikiService):
    generated = service.generate("Classification Target")["article"]
    preview = service.classification_preview([
        {
            "article_id": generated["article_id"],
            "article_revision": generated["revision"],
            "decision": "new",
            "new_category": {"client_ref": "research", "name": "Research", "description": "Research notes"},
            "tags": ["AI", "Workflow"],
        }
    ])
    assert preview["can_commit"] is True, preview
    assert not (service.root / "wiki" / "Research").exists()

    committed = service.classification_commit(preview["preview_id"])
    moved = committed["moved_articles"][0]
    assert moved["path"] == "Research/Classification-Target.md"
    assert moved["classification_status"] == "confirmed"
    assert moved["tags"] == ["AI", "Workflow"]
    assert moved["primary_category_id"] == committed["created_categories"][0]["category_id"]

    service.rollback(committed["operation_id"])
    assert service.read_article("_inbox/Classification-Target.md")["classification_status"] == "pending"
    assert not (service.root / "wiki" / "Research").exists()


def test_preview_rejects_stale_article_revision(service: WikiService):
    article = service.generate("Stale Target")["article"]
    category = service.categories()[0]
    preview = service.classification_preview([
        {"article_id": article["article_id"], "article_revision": "0" * 64, "decision": "existing", "category_id": category["category_id"]}
    ])
    assert preview["can_commit"] is False
    assert preview["conflicts"][0]["kind"] == "article_changed"


def test_category_rename_archive_restore_and_empty_delete(service: WikiService):
    created_preview = service.category_preview("create", name="Notes", description="Personal notes")
    created = service.category_commit(created_preview["preview_id"])
    category = created["category"]
    assert (service.root / "wiki" / "Notes").is_dir()

    rename_preview = service.category_preview("rename", category_id=category["category_id"], name="Reference")
    renamed = service.category_commit(rename_preview["preview_id"])
    assert renamed["category"]["category_id"] == category["category_id"]
    assert (service.root / "wiki" / "Reference").is_dir()
    assert not (service.root / "wiki" / "Notes").exists()

    archived = service.category_commit(service.category_preview("archive", category_id=category["category_id"])["preview_id"])
    assert archived["category"]["status"] == "archived"
    restored = service.category_commit(service.category_preview("restore", category_id=category["category_id"])["preview_id"])
    assert restored["category"]["status"] == "active"
    service.category_commit(service.category_preview("delete", category_id=category["category_id"])["preview_id"])
    assert not (service.root / "wiki" / "Reference").exists()


def test_category_management_can_batch_migrate_articles(service: WikiService):
    source = next(item for item in service.categories() if item["directory_name"] == "concepts")
    target = next(item for item in service.categories() if item["directory_name"] == "tools")
    generated = service.generate("Managed Migration")["article"]
    classified = service.classification_preview([
        {"article_id": generated["article_id"], "article_revision": generated["revision"], "decision": "existing", "category_id": source["category_id"]}
    ])
    service.classification_commit(classified["preview_id"])
    before_revision = dc.load_registry(service.root)["revision"]
    preview = service.category_preview(
        "migrate",
        category_id=source["category_id"],
        target_category_id=target["category_id"],
    )
    assert preview["can_commit"] is True, preview
    assert preview["moves"]
    result = service.category_commit(preview["preview_id"])
    assert result["operation_id"]
    assert dc.load_registry(service.root)["revision"] == before_revision
    assert all(item["primary_category_id"] == target["category_id"] for item in result["moved_articles"])
    service.rollback(result["operation_id"])
    assert any(item["primary_category_id"] == source["category_id"] for item in service.articles())


def test_external_directory_only_creates_reconciliation_item(service: WikiService):
    external = service.root / "wiki" / "External"
    external.mkdir()
    result = service.scan_reconciliation()
    assert external.is_dir()
    assert any(item["kind"] == "new_directory" and item["payload"]["path"] == "External" for item in result["items"])


def test_adopt_external_directory_is_explicit_and_adds_stable_metadata(service: WikiService):
    external = service.root / "wiki" / "External"
    external.mkdir()
    (external / "note.md").write_text("# External Note\n\n> Category: External\n> Status: 词条\n\nBody\n", encoding="utf-8")
    item = next(row for row in service.scan_reconciliation()["items"] if row["kind"] == "new_directory")
    preview = service.reconciliation_preview(item["id"], "adopt")
    assert preview["can_commit"] is True
    assert not any(category["directory_name"] == "External" for category in service.categories())
    result = service.reconciliation_commit(preview["preview_id"])
    article = service.read_article("External/note.md")
    assert result["operation_id"]
    assert article["classification_status"] == "confirmed"
    assert article["primary_category_id"]


def test_nonempty_category_delete_is_blocked(service: WikiService):
    category = next(item for item in service.categories() if item["directory_name"] == "concepts")
    preview = service.category_preview("delete", category_id=category["category_id"])
    assert preview["can_commit"] is False
    assert {item["kind"] for item in preview["conflicts"]} >= {"directory_not_empty"}


def test_article_id_cannot_be_changed_by_editor(service: WikiService):
    article = service.read_article("concepts/base.md")
    tampered = article["markdown"].replace(article["article_id"], "f" * 32)
    with pytest.raises(ValueError, match="immutable"):
        service.save_article(article["path"], tampered, article["revision"])


def test_normalized_category_names_conflict(service: WikiService):
    created = service.category_commit(service.category_preview("create", name="ＡＩ Notes")["preview_id"])
    assert created["category"]["name"] == "AI Notes"
    with pytest.raises(ValueError, match="already exists"):
        service.category_preview("create", name="ai notes")


def test_raw_preview_never_creates_expected_category_directory(service: WikiService):
    raw = service.root / "raw" / "local" / "plan.txt"
    raw.write_text("A completely new knowledge domain with enough body text for planning.", encoding="utf-8")
    before = {path.name for path in (service.root / "wiki").iterdir() if path.is_dir()}
    preview = service.ingest_preview("raw/local/plan.txt")
    after = {path.name for path in (service.root / "wiki").iterdir() if path.is_dir()}
    assert preview["classification_plan"]["notice"]
    assert after == before


def test_projection_failure_rolls_back_classification_files(service: WikiService, monkeypatch: pytest.MonkeyPatch):
    article = service.generate("Projection Failure")["article"]
    category = service.categories()[0]
    preview = service.classification_preview([{
        "article_id": article["article_id"], "article_revision": article["revision"],
        "decision": "existing", "category_id": category["category_id"], "tags": [],
    }])
    monkeypatch.setattr(service.state, "remap_article_path", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("projection failed")))
    with pytest.raises(RuntimeError, match="projection failed"):
        service.classification_commit(preview["preview_id"])
    assert service.read_article(article["path"])["classification_status"] == "pending"
    assert not (service.root / "wiki" / category["directory_name"] / Path(article["path"]).name).exists()


def test_classification_draft_survives_service_restart(kb_root: Path):
    first = WikiService(kb_root, start_worker=False)
    article = first.generate("Persistent Draft")["article"]
    category = first.categories()[0]
    selection = {"article_id": article["article_id"], "article_revision": article["revision"], "decision": "existing", "category_id": category["category_id"], "tags": ["draft"]}
    saved = first.save_classification_draft([selection], 0)
    first.close()

    reopened = WikiService(kb_root, start_worker=False)
    try:
        workbench = reopened.classification_workbench()
        assert workbench["draft"]["revision"] == saved["revision"]
        assert workbench["draft"]["selections"] == [selection]
    finally:
        reopened.close()


def test_classification_preview_blocks_duplicate_articles_and_target_paths(service: WikiService):
    first = service.generate("Shared Name")["article"]
    second_path = service.root / "wiki" / "_inbox" / "nested" / "Shared-Name.md"
    second_path.parent.mkdir(parents=True)
    second_path.write_text(
        dc.ensure_article_metadata(
            "# Other Shared Name\n\nBody from the second article.\n",
            category_id=None,
            status="pending",
            article_uuid=uuid.uuid4().hex,
            article_tags=[],
        ),
        encoding="utf-8",
    )
    second = service.read_article("_inbox/nested/Shared-Name.md")
    category = service.categories()[0]
    duplicate_article = service.classification_preview([
        {"article_id": first["article_id"], "article_revision": first["revision"], "decision": "existing", "category_id": category["category_id"]},
        {"article_id": first["article_id"], "article_revision": first["revision"], "decision": "existing", "category_id": category["category_id"]},
    ])
    assert duplicate_article["can_commit"] is False
    assert {item["kind"] for item in duplicate_article["conflicts"]} == {"duplicate_article_selection"}

    collision = service.classification_preview([
        {"article_id": first["article_id"], "article_revision": first["revision"], "decision": "existing", "category_id": category["category_id"]},
        {"article_id": second["article_id"], "article_revision": second["revision"], "decision": "existing", "category_id": category["category_id"]},
    ])
    assert collision["can_commit"] is False
    assert "duplicate_target_path" in {item["kind"] for item in collision["conflicts"]}
    with pytest.raises(RuntimeError, match="duplicate paths"):
        service.classification_commit(collision["preview_id"])
    assert service.read_article(first["path"])["markdown"] == first["markdown"]
    assert service.read_article(second["path"])["markdown"] == second["markdown"]
    assert not (service.root / "wiki" / category["directory_name"] / "Shared-Name.md").exists()


def test_existing_category_confirmation_preserves_taxonomy_and_other_suggestions(service: WikiService):
    first = service.generate("Confirm One")["article"]
    second = service.generate("Keep Suggestion")["article"]
    registry = dc.load_registry(service.root)
    category = service.categories()[0]
    service.state.save_classification_suggestion(
        second["article_id"], second["revision"], registry["revision"], "succeeded",
        suggestion={"candidates": [{"category_id": category["category_id"], "confidence": 0.9, "reason": "stable"}], "tags": [], "new_category": None},
    )
    preview = service.classification_preview([
        {"article_id": first["article_id"], "article_revision": first["revision"], "decision": "existing", "category_id": category["category_id"]}
    ])
    service.classification_commit(preview["preview_id"])
    assert dc.load_registry(service.root)["revision"] == registry["revision"]
    remaining = next(item for item in service.classification_workbench()["items"] if item["article"]["article_id"] == second["article_id"])
    assert remaining["suggestion"]["status"] == "succeeded"


def test_reconciliation_scan_removes_disappeared_pending_items(service: WikiService):
    external = service.root / "wiki" / "Transient"
    external.mkdir()
    assert service.scan_reconciliation()["count"] == 1
    external.rmdir()
    result = service.scan_reconciliation()
    assert result["count"] == 0
    assert service.state.list_reconciliation() == []


def test_concurrent_directory_adoption_preserves_both_categories(service: WikiService):
    for name in ("ExternalA", "ExternalB"):
        folder = service.root / "wiki" / name
        folder.mkdir()
        (folder / "note.md").write_text(f"# {name}\n\nBody\n", encoding="utf-8")
    items = {item["payload"]["path"]: item for item in service.scan_reconciliation()["items"]}
    previews = [service.reconciliation_preview(items[name]["id"], "adopt")["preview_id"] for name in ("ExternalA", "ExternalB")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(service.reconciliation_commit, previews))
    assert all(item["operation_id"] for item in results)
    registry = dc.load_registry(service.root)
    adopted = {item["directory_name"]: item for item in registry["categories"] if item["directory_name"].startswith("External")}
    assert set(adopted) == {"ExternalA", "ExternalB"}
    for name, category in adopted.items():
        article = service.read_article(f"{name}/note.md")
        assert article["primary_category_id"] == category["category_id"]


def test_late_classification_and_raw_results_do_not_persist(service: WikiService, monkeypatch: pytest.MonkeyPatch):
    article = service.generate("Late Classification")["article"]
    registry = dc.load_registry(service.root)
    article_task, _ = service.state.enqueue_task(
        "article-classification",
        article["article_id"],
        {"path": article["path"], "article_id": article["article_id"], "article_revision": article["revision"], "taxonomy_revision": registry["revision"]},
    )
    article_task = service.state.claim_task({"article-classification"}) or article_task
    monkeypatch.setattr(service, "_call_classification_llm", lambda *_args: (
        service.state.cancel_task(article_task["id"]),
        {"candidates": [], "tags": [], "new_category": None},
    )[1])
    article_result = service._run_classification_task(article_task)
    assert article_result["cancelled"] is True
    assert service._finalize_remote_result(article_task, article_result)["status"] == "cancelled"
    assert service.state.classification_suggestion(article["article_id"], article["revision"], registry["revision"]) is None

    raw_path = service.root / "raw" / "local" / "late.txt"
    raw_path.write_text("Raw body for a delayed category plan.", encoding="utf-8")
    raw = service.read_raw("raw/local/late.txt")
    raw_task, _ = service.state.enqueue_task(
        "raw-classification-plan",
        raw["path"],
        {"raw_path": raw["path"], "raw_revision": raw["revision"], "taxonomy_revision": registry["revision"]},
    )
    raw_task = service.state.claim_task({"raw-classification-plan"}) or raw_task
    monkeypatch.setattr(service, "_call_raw_plan_llm", lambda *_args: (
        service.category_commit(service.category_preview("create", name="Changed During Model")["preview_id"]),
        {"candidates": []},
    )[1])
    raw_result = service._run_raw_classification_plan(raw_task)
    assert raw_result["stale"] is True
    assert service._finalize_remote_result(raw_task, raw_result)["status"] == "failed"
    plan = service.state.raw_classification_plan(raw["path"], raw["revision"], registry["revision"])
    assert plan is None or plan["status"] != "succeeded"


def test_cancel_cannot_split_successful_classification_finalization(service: WikiService, monkeypatch: pytest.MonkeyPatch):
    article = service.generate("Atomic Classification")["article"]
    registry = dc.load_registry(service.root)
    task, _ = service.state.enqueue_task(
        "article-classification",
        article["article_id"],
        {"path": article["path"], "article_id": article["article_id"], "article_revision": article["revision"], "taxonomy_revision": registry["revision"]},
    )
    task = service.state.claim_task({"article-classification"}) or task
    entered = Event()
    release = Event()
    cancel_reached_state = Event()
    original_finalize = service.state.finalize_classification_success
    original_cancel = service.state.cancel_task

    def blocked_finalize(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original_finalize(*args, **kwargs)

    def observed_cancel(task_id: str):
        cancel_reached_state.set()
        return original_cancel(task_id)

    monkeypatch.setattr(service.state, "finalize_classification_success", blocked_finalize)
    monkeypatch.setattr(service.state, "cancel_task", observed_cancel)
    result = {"stale": False, "article_id": article["article_id"], "suggestion": {"candidates": [], "tags": [], "new_category": None}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        finalize_future = pool.submit(service._finalize_remote_result, task, result)
        assert entered.wait(2)
        cancel_future = pool.submit(service.cancel_task, task["id"])
        assert not cancel_reached_state.wait(0.1)
        release.set()
        assert finalize_future.result(timeout=2)["status"] == "succeeded"
        assert cancel_future.result(timeout=2)["status"] == "succeeded"
    suggestion = service.state.classification_suggestion(article["article_id"], article["revision"], registry["revision"])
    assert suggestion and suggestion["status"] == "succeeded"


def test_cancel_cannot_split_successful_raw_plan_finalization(service: WikiService, monkeypatch: pytest.MonkeyPatch):
    raw_file = service.root / "raw" / "local" / "atomic-plan.txt"
    raw_file.write_text("Raw body for an atomic classification plan.", encoding="utf-8")
    raw = service.read_raw("raw/local/atomic-plan.txt")
    registry = dc.load_registry(service.root)
    task, _ = service.state.enqueue_task(
        "raw-classification-plan",
        raw["path"],
        {"raw_path": raw["path"], "raw_revision": raw["revision"], "taxonomy_revision": registry["revision"]},
    )
    task = service.state.claim_task({"raw-classification-plan"}) or task
    entered = Event()
    release = Event()
    cancel_reached_state = Event()
    original_finalize = service.state.finalize_raw_classification_success
    original_cancel = service.state.cancel_task

    def blocked_finalize(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original_finalize(*args, **kwargs)

    def observed_cancel(task_id: str):
        cancel_reached_state.set()
        return original_cancel(task_id)

    monkeypatch.setattr(service.state, "finalize_raw_classification_success", blocked_finalize)
    monkeypatch.setattr(service.state, "cancel_task", observed_cancel)
    result = {"stale": False, "raw_path": raw["path"], "plan": {"candidates": []}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        finalize_future = pool.submit(service._finalize_remote_result, task, result)
        assert entered.wait(2)
        cancel_future = pool.submit(service.cancel_task, task["id"])
        assert not cancel_reached_state.wait(0.1)
        release.set()
        assert finalize_future.result(timeout=2)["status"] == "succeeded"
        assert cancel_future.result(timeout=2)["status"] == "succeeded"
    plan = service.state.raw_classification_plan(raw["path"], raw["revision"], registry["revision"])
    assert plan and plan["status"] == "succeeded"


def test_old_classification_attempt_cannot_finalize_retried_task(service: WikiService):
    article = service.generate("Attempt Fence")["article"]
    registry = dc.load_registry(service.root)
    task, _ = service.state.enqueue_task(
        "article-classification",
        article["article_id"],
        {"path": article["path"], "article_id": article["article_id"], "article_revision": article["revision"], "taxonomy_revision": registry["revision"]},
    )
    first_attempt = service.state.claim_task({"article-classification"}) or task
    service.cancel_task(first_attempt["id"])
    service.retry_task(first_attempt["id"])
    second_attempt = service.state.claim_task({"article-classification"})
    assert second_attempt and second_attempt["attempts"] == first_attempt["attempts"] + 1
    old_result = {"stale": False, "article_id": article["article_id"], "suggestion": {"candidates": [], "tags": ["old"], "new_category": None}}
    assert service._finalize_remote_result(first_attempt, old_result)["status"] == "running"
    suggestion = service.state.classification_suggestion(article["article_id"], article["revision"], registry["revision"])
    assert suggestion is None or suggestion["status"] != "succeeded"
