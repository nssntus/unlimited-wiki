from __future__ import annotations

from pathlib import Path

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
    assert preview["can_commit"] is True
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
