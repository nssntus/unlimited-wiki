from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

import dynamic_categories as dc
import storage
import wiki_service as wiki_service_module
from state_store import StateStore, now_iso
from wiki_service import WikiService


@pytest.fixture
def service(kb_root: Path):
    instance = WikiService(kb_root, start_worker=False)
    try:
        yield instance
    finally:
        instance.close()


def test_migration_adds_stable_ids_and_keeps_uncategorized_in_inbox(kb_root: Path):
    first = WikiService(kb_root, start_worker=False)
    try:
        article = first.read_article("concepts/base.md")
        assert len(article["article_id"]) == 32
        assert article["primary_category_id"] is None
        stable_id = article["article_id"]
    finally:
        first.close()
    reopened = WikiService(kb_root, start_worker=False)
    try:
        assert reopened.read_article("concepts/base.md")["article_id"] == stable_id
    finally:
        reopened.close()


def test_generate_can_save_to_inbox_or_create_category_with_tags(service: WikiService):
    inbox = service.generate("Inbox Topic")["article"]
    assert inbox["path"] == "_inbox/Inbox-Topic.md"
    assert inbox["primary_category_id"] is None

    created = service.generate(
        "Instant Topic",
        category={"kind": "create", "name": "Research Notes"},
        tags=["AI", "Workflow"],
    )["article"]
    assert created["path"] == "Research-Notes/Instant-Topic.md"
    assert created["category_label"] == "Research Notes"
    assert created["tags"] == ["AI", "Workflow"]
    assert (service.root / "wiki" / "Research-Notes").is_dir()


def test_manual_article_create_is_atomic_and_replays_committed_result(service: WikiService):
    result = service.create_article(
        "Manual Knowledge",
        "## 概述\n\n全部内容由用户手动输入。\n\n- 第一项\n- 第二项",
        category={"kind": "create", "name": "Manual Notes"},
        tags=["Writing", "writing"],
    )
    article = result["article"]

    assert result["replayed"] is False
    assert result["created_category"] is True
    assert article["path"] == "Manual-Notes/Manual-Knowledge.md"
    assert article["category_label"] == "Manual Notes"
    assert article["content_status"] == "草稿"
    assert article["classification_status"] == "confirmed"
    assert article["tags"] == ["Writing"]
    assert len(article["article_id"]) == 32
    assert "全部内容由用户手动输入" in article["markdown"]
    assert "Manual Knowledge" in (service.root / "wiki" / "index.md").read_text(encoding="utf-8")

    replay = service.create_article(
        "Manual Knowledge",
        "## 概述\n\n全部内容由用户手动输入。\n\n- 第一项\n- 第二项",
        category={"kind": "create", "name": "Manual Notes"},
        tags=["Writing", "writing"],
    )
    assert replay["replayed"] is True
    assert replay["operation_id"] == result["operation_id"]
    assert replay["article"]["article_id"] == article["article_id"]


def test_manual_article_create_rolls_back_new_category_and_retries(service: WikiService, monkeypatch: pytest.MonkeyPatch):
    registry_before = (service.root / dc.REGISTRY_REL).read_bytes()
    original_write = storage.atomic_write
    failed = False

    def fail_once(path: Path, data: bytes):
        nonlocal failed
        if not failed and path.name == "log.md":
            failed = True
            raise OSError("injected manual create failure")
        return original_write(path, data)

    monkeypatch.setattr(storage, "atomic_write", fail_once)
    with pytest.raises(OSError, match="manual create failure"):
        service.create_article(
            "Retry Manual",
            "正文。",
            category={"kind": "create", "name": "Retry Manual Category"},
            tags=["retry"],
        )

    assert (service.root / dc.REGISTRY_REL).read_bytes() == registry_before
    assert not (service.root / "wiki" / "Retry-Manual-Category").exists()
    assert not any(item["title"] == "Retry Manual" for item in service.articles())

    result = service.create_article(
        "Retry Manual",
        "正文。",
        category={"kind": "create", "name": "Retry Manual Category"},
        tags=["retry"],
    )
    assert result["operation_id"].endswith("-attempt-2")
    assert result["article"]["path"] == "Retry-Manual-Category/Retry-Manual.md"


def test_manual_article_replay_rejects_target_replaced_by_redirect(service: WikiService):
    created = service.create_article(
        "Redirect Repair Manual",
        "Original body.",
        category={"kind": "inbox"},
        tags=[],
    )
    target = service.root / "wiki" / created["article"]["path"]
    target.write_text("# Redirect Repair Manual\n\n> Redirect: ../concepts/base.md\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="requires repair"):
        service.create_article(
            "Redirect Repair Manual",
            "Original body.",
            category={"kind": "inbox"},
            tags=[],
        )


def test_manual_article_create_never_overwrites_physical_or_alias_collision(service: WikiService):
    target = service.root / "wiki" / "_inbox" / "Physical-Manual.md"
    target.parent.mkdir(exist_ok=True)
    target.write_text("# Different title\n\nORIGINAL SENTINEL\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="already exists"):
        service.create_article("Physical Manual", "Replacement body.", category={"kind": "inbox"}, tags=[])
    assert "ORIGINAL SENTINEL" in target.read_text(encoding="utf-8")

    with pytest.raises(FileExistsError, match="already exists"):
        service.create_article("Base", "Replacement body.", category={"kind": "inbox"}, tags=[])
    assert "Replacement body" not in service.read_article("concepts/base.md")["markdown"]


def test_concurrent_manual_article_title_collision_has_one_writer(service: WikiService):
    def create(body: str):
        try:
            return service.create_article("Concurrent Manual", body, category={"kind": "inbox"}, tags=[])
        except FileExistsError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ("First body", "Second body")))

    created = [item for item in results if isinstance(item, dict)]
    rejected = [item for item in results if isinstance(item, FileExistsError)]
    assert len(created) == len(rejected) == 1
    article = service.read_article("_inbox/Concurrent-Manual.md")
    assert ("First body" in article["markdown"]) != ("Second body" in article["markdown"])


@pytest.mark.parametrize(
    "body",
    [
        "",
        "# Nested title\n\nBody",
        "  # Indented title\n\nBody",
        "Setext title\n===\n\nBody",
        "> Article-ID: forged\n\nBody",
        "> Redirect: ../concepts/base.md\n\nBody",
    ],
)
def test_manual_article_rejects_empty_h1_and_managed_metadata(service: WikiService, body: str):
    with pytest.raises(ValueError):
        service.create_article("Invalid Manual", body, category={"kind": "inbox"}, tags=[])
    assert not (service.root / "wiki" / "_inbox" / "Invalid-Manual.md").exists()


@pytest.mark.parametrize("fence", ["`", "~"])
def test_manual_article_allows_h1_examples_inside_longer_markdown_fences(
    service: WikiService, fence: str,
):
    outer = fence * 4
    inner = fence * 3
    body = f"""## Fence example

{outer}markdown
{inner}markdown
# Example H1
{inner}
{outer}

Regular body.
"""
    result = service.create_article("Fence Manual", body, category={"kind": "inbox"}, tags=[])
    assert result["article"]["path"] == "_inbox/Fence-Manual.md"
    assert "# Example H1" in result["article"]["markdown"]


def test_manual_article_creation_serializes_planning_across_service_instances(
    service: WikiService, monkeypatch: pytest.MonkeyPatch,
):
    second = WikiService(service.root, start_worker=False)
    first_planning = threading.Event()
    release_first = threading.Event()
    second_planning = threading.Event()
    first_plan = service._plan_taxonomy
    second_plan = second._plan_taxonomy

    def pause_first(*args, **kwargs):
        first_planning.set()
        assert release_first.wait(timeout=5)
        return first_plan(*args, **kwargs)

    def observe_second(*args, **kwargs):
        second_planning.set()
        return second_plan(*args, **kwargs)

    monkeypatch.setattr(service, "_plan_taxonomy", pause_first)
    monkeypatch.setattr(second, "_plan_taxonomy", observe_second)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(
                service.create_article,
                "First Shared Manual",
                "First body.",
                category={"kind": "create", "name": "First Shared Category"},
                tags=["first"],
            )
            assert first_planning.wait(timeout=5)
            second_future = pool.submit(
                second.create_article,
                "Second Shared Manual",
                "Second body.",
                category={"kind": "create", "name": "Second Shared Category"},
                tags=["second"],
            )
            assert not second_planning.wait(timeout=0.2)
            release_first.set()
            first_result = first_future.result(timeout=5)
            second_result = second_future.result(timeout=5)
    finally:
        release_first.set()
        second.close()

    registry = dc.load_registry(service.root)
    category_ids = {item["category_id"] for item in registry["categories"]}
    assert first_result["article"]["primary_category_id"] in category_ids
    assert second_result["article"]["primary_category_id"] in category_ids
    index = (service.root / "wiki" / "index.md").read_text(encoding="utf-8")
    log = (service.root / "wiki" / "log.md").read_text(encoding="utf-8")
    assert "First Shared Manual" in index and "Second Shared Manual" in index
    assert first_result["operation_id"] in log and second_result["operation_id"] in log


def test_manual_article_and_apply_meta_share_cross_instance_planning_lock(
    service: WikiService, monkeypatch: pytest.MonkeyPatch,
):
    second = WikiService(service.root, start_worker=False)
    governance_planning = threading.Event()
    release_governance = threading.Event()
    manual_planning = threading.Event()
    governance_plan = service._plan_taxonomy
    manual_plan = second._plan_taxonomy

    def pause_governance(*args, **kwargs):
        governance_planning.set()
        assert release_governance.wait(timeout=5)
        return governance_plan(*args, **kwargs)

    def observe_manual(*args, **kwargs):
        manual_planning.set()
        return manual_plan(*args, **kwargs)

    monkeypatch.setattr(service, "_plan_taxonomy", pause_governance)
    monkeypatch.setattr(second, "_plan_taxonomy", observe_manual)
    try:
        base = service.read_article("concepts/base.md")
        with ThreadPoolExecutor(max_workers=2) as pool:
            governance_future = pool.submit(
                service.apply_meta,
                "concepts/base.md",
                category={"kind": "create", "name": "Governed Shared Category"},
                status="词条",
                tags=["governed"],
                expected_revision=base["revision"],
            )
            assert governance_planning.wait(timeout=5)
            manual_future = pool.submit(
                second.create_article,
                "Manual During Governance",
                "Manual body.",
                category={"kind": "create", "name": "Manual Shared Category"},
                tags=["manual"],
            )
            assert not manual_planning.wait(timeout=0.2)
            release_governance.set()
            governance_result = governance_future.result(timeout=5)
            manual_result = manual_future.result(timeout=5)
    finally:
        release_governance.set()
        second.close()

    registry = dc.load_registry(service.root)
    category_ids = {item["category_id"] for item in registry["categories"]}
    assert governance_result["article"]["primary_category_id"] in category_ids
    assert manual_result["article"]["primary_category_id"] in category_ids
    index = (service.root / "wiki" / "index.md").read_text(encoding="utf-8")
    log = (service.root / "wiki" / "log.md").read_text(encoding="utf-8")
    assert "Base" in index and "Manual During Governance" in index
    assert governance_result["operation_id"] in log and manual_result["operation_id"] in log


def test_manual_article_and_save_share_cross_instance_planning_lock(
    service: WikiService, monkeypatch: pytest.MonkeyPatch,
):
    second = WikiService(service.root, start_worker=False)
    manual_planning = threading.Event()
    release_manual = threading.Event()
    save_started = threading.Event()
    save_planning = threading.Event()
    manual_plan = service._plan_taxonomy
    render_index = wiki_service_module.render_index

    def pause_manual(*args, **kwargs):
        manual_planning.set()
        assert release_manual.wait(timeout=5)
        return manual_plan(*args, **kwargs)

    def observe_save(root, overrides=None):
        if overrides and "wiki/concepts/base.md" in overrides:
            save_planning.set()
        return render_index(root, overrides)

    monkeypatch.setattr(service, "_plan_taxonomy", pause_manual)
    monkeypatch.setattr(wiki_service_module, "render_index", observe_save)
    try:
        base = second.read_article("concepts/base.md")
        edited = base["markdown"].rstrip() + "\n\nSaved concurrently.\n"

        def save_base():
            save_started.set()
            return second.save_article("concepts/base.md", edited, base["revision"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            manual_future = pool.submit(
                service.create_article,
                "Manual During Save",
                "Manual body.",
                category={"kind": "create", "name": "Manual Save Category"},
                tags=[],
            )
            assert manual_planning.wait(timeout=5)
            save_future = pool.submit(save_base)
            assert save_started.wait(timeout=5)
            assert not save_planning.wait(timeout=0.2)
            release_manual.set()
            manual_result = manual_future.result(timeout=5)
            save_result = save_future.result(timeout=5)
    finally:
        release_manual.set()
        second.close()

    assert "Saved concurrently." in service.read_article("concepts/base.md")["markdown"]
    index = (service.root / "wiki" / "index.md").read_text(encoding="utf-8")
    log = (service.root / "wiki" / "log.md").read_text(encoding="utf-8")
    assert "Manual During Save" in index
    assert manual_result["operation_id"] in log and save_result["operation_id"] in log


def test_manual_article_and_ingest_share_cross_instance_planning_lock(
    service: WikiService, monkeypatch: pytest.MonkeyPatch,
):
    second = WikiService(service.root, start_worker=False)
    raw_path = service.root / "raw" / "local" / "concurrent-ingest.txt"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text("# Concurrent source\n\nRaw body.\n", encoding="utf-8")
    manual_planning = threading.Event()
    release_manual = threading.Event()
    ingest_started = threading.Event()
    ingest_planning = threading.Event()
    manual_plan = service._plan_taxonomy
    ingest_plan = second._plan_taxonomy

    def pause_manual(*args, **kwargs):
        manual_planning.set()
        assert release_manual.wait(timeout=5)
        return manual_plan(*args, **kwargs)

    def observe_ingest(*args, **kwargs):
        ingest_planning.set()
        return ingest_plan(*args, **kwargs)

    monkeypatch.setattr(service, "_plan_taxonomy", pause_manual)
    monkeypatch.setattr(second, "_plan_taxonomy", observe_ingest)
    try:
        def ingest_source():
            ingest_started.set()
            return second.ingest_commit(
                "raw/local/concurrent-ingest.txt",
                "new",
                title="Ingest During Manual",
                category={"kind": "create", "name": "Ingest Shared Category"},
                tags=["ingest"],
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            manual_future = pool.submit(
                service.create_article,
                "Manual During Ingest",
                "Manual body.",
                category={"kind": "create", "name": "Manual Ingest Category"},
                tags=["manual"],
            )
            assert manual_planning.wait(timeout=5)
            ingest_future = pool.submit(ingest_source)
            assert ingest_started.wait(timeout=5)
            assert not ingest_planning.wait(timeout=0.2)
            release_manual.set()
            manual_result = manual_future.result(timeout=5)
            ingest_result = ingest_future.result(timeout=5)
    finally:
        release_manual.set()
        second.close()

    registry = dc.load_registry(service.root)
    category_ids = {item["category_id"] for item in registry["categories"]}
    assert manual_result["article"]["primary_category_id"] in category_ids
    assert ingest_result["article"]["primary_category_id"] in category_ids
    index = (service.root / "wiki" / "index.md").read_text(encoding="utf-8")
    log = (service.root / "wiki" / "log.md").read_text(encoding="utf-8")
    assert "Manual During Ingest" in index and "Ingest During Manual" in index
    assert manual_result["operation_id"] in log and ingest_result["operation_id"] in log


def test_inline_category_failure_rolls_back_directory_registry_and_article(service: WikiService, monkeypatch: pytest.MonkeyPatch):
    registry_before = (service.root / dc.REGISTRY_REL).read_bytes()
    original_write = storage.atomic_write

    def fail_article(path: Path, data: bytes):
        if "Rollback-Category" in path.parts and path.suffix == ".md":
            raise OSError("injected article write failure")
        return original_write(path, data)

    monkeypatch.setattr(storage, "atomic_write", fail_article)
    with pytest.raises(OSError, match="injected"):
        service.generate(
            "Rollback Topic",
            category={"kind": "create", "name": "Rollback Category"},
            tags=["atomic"],
        )
    assert (service.root / dc.REGISTRY_REL).read_bytes() == registry_before
    assert not (service.root / "wiki" / "Rollback-Category").exists()
    assert not any(item["title"] == "Rollback Topic" for item in service.articles())


def test_article_taxonomy_reuses_normalized_names_and_checks_revision(service: WikiService):
    article = service.generate("Move Inline")["article"]
    first = service.apply_meta(
        article["path"],
        category={"kind": "create", "name": "ＡＩ Notes"},
        status="词条",
        tags=["Ｃｏｄｅｘ", "codex"],
        expected_revision=article["revision"],
    )
    assert first["article"]["category_label"] == "AI Notes"
    assert first["article"]["tags"] == ["Codex"]
    assert first["created_category"] is True

    other = service.generate("Reuse Inline")["article"]
    second = service.apply_meta(
        other["path"],
        category={"kind": "create", "name": "ai notes"},
        status="词条",
        tags=["codex"],
        expected_revision=other["revision"],
    )
    assert second["created_category"] is False
    assert second["article"]["primary_category_id"] == first["article"]["primary_category_id"]
    stale = service.apply_meta(
        second["article"]["path"],
        category={"kind": "inbox"},
        status="词条",
        tags=[],
        expected_revision="0" * 64,
    )
    assert stale["conflict"] is True
    assert service.read_article(second["article"]["path"])["primary_category_id"] == first["article"]["primary_category_id"]


def test_raw_preview_has_no_ai_classification_and_commit_is_atomic(service: WikiService):
    raw = service.root / "raw" / "local" / "instant.txt"
    raw.write_text("# Instant Raw\n\nGrounded body for the new article.\n", encoding="utf-8")
    preview = service.ingest_preview("raw/local/instant.txt")
    assert not ({"classification_plan", "suggested_category", "candidates", "confidence"} & set(preview))
    result = service.ingest_commit(
        "raw/local/instant.txt",
        "new",
        title="Instant Raw",
        category={"kind": "create", "name": "Raw Research"},
        tags=["Evidence"],
    )
    assert result["article"]["path"] == "Raw-Research/Instant-Raw.md"
    assert result["article"]["tags"] == ["Evidence"]


def test_seed_rejects_existing_article_without_taxonomy_side_effects(service: WikiService):
    raw = service.root / "raw" / "local" / "existing-seed.md"
    raw.write_text("# Base\n\nReplacement body.\n", encoding="utf-8")
    original = (service.root / "wiki" / "concepts" / "base.md").read_bytes()
    registry = (service.root / dc.REGISTRY_REL).read_bytes()
    directories = sorted(path.name for path in (service.root / "wiki").iterdir() if path.is_dir())

    with pytest.raises(ValueError, match="use supplement"):
        service.ingest_commit(
            "raw/local/existing-seed.md", "seed", title="Base",
            category={"kind": "create", "name": "Should Not Exist"}, tags=["ignored"],
        )

    assert (service.root / "wiki" / "concepts" / "base.md").read_bytes() == original
    assert (service.root / dc.REGISTRY_REL).read_bytes() == registry
    assert sorted(path.name for path in (service.root / "wiki").iterdir() if path.is_dir()) == directories
    assert not any(row["path"] == "raw/local/existing-seed.md" for row in service.state.raw_records())


def test_seed_rejects_physical_slug_collision_without_overwriting(service: WikiService):
    target = service.root / "wiki" / "_inbox" / "Foo.md"
    target.parent.mkdir(exist_ok=True)
    target.write_text("# Different Title\n\nORIGINAL SENTINEL\n", encoding="utf-8")
    raw = service.root / "raw" / "local" / "physical-collision.md"
    raw.write_text("# Foo\n\nReplacement body.\n", encoding="utf-8")
    before = {
        "target": target.read_bytes(),
        "registry": (service.root / dc.REGISTRY_REL).read_bytes(),
        "index": (service.root / "wiki" / "index.md").read_bytes(),
        "log": (service.root / "wiki" / "log.md").read_bytes(),
        "history": {path.name for path in service.files.history_root.iterdir()},
    }

    with pytest.raises(ValueError, match="use supplement"):
        service.ingest_commit("raw/local/physical-collision.md", "seed", title="Foo")

    assert target.read_bytes() == before["target"]
    assert (service.root / dc.REGISTRY_REL).read_bytes() == before["registry"]
    assert (service.root / "wiki" / "index.md").read_bytes() == before["index"]
    assert (service.root / "wiki" / "log.md").read_bytes() == before["log"]
    assert {path.name for path in service.files.history_root.iterdir()} == before["history"]
    assert not any(row["path"] == "raw/local/physical-collision.md" for row in service.state.raw_records())


def test_seed_commit_rechecks_target_absence_under_file_lock(service: WikiService, monkeypatch: pytest.MonkeyPatch):
    raw = service.root / "raw" / "local" / "seed-race.md"
    raw.write_text("# Seed Race\n\nReplacement body.\n", encoding="utf-8")
    target = service.root / "wiki" / "_inbox" / "Seed-Race.md"
    target.parent.mkdir(exist_ok=True)
    original_commit = service.files.commit

    def create_target_before_commit(*args, **kwargs):
        target.write_text("# Concurrent Writer\n\nRACE SENTINEL\n", encoding="utf-8")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(service.files, "commit", create_target_before_commit)
    with pytest.raises(ValueError, match="use supplement"):
        service.ingest_commit("raw/local/seed-race.md", "seed", title="Seed Race")

    assert "RACE SENTINEL" in target.read_text(encoding="utf-8")
    assert not any(row["path"] == "raw/local/seed-race.md" for row in service.state.raw_records())
    assert not any(path.name.startswith("ingest-") for path in service.files.history_root.iterdir())


def test_ingest_file_failure_retries_with_next_operation_attempt(service: WikiService, monkeypatch: pytest.MonkeyPatch):
    raw = service.root / "raw" / "local" / "retry-ingest.md"
    raw.write_text("# Retry Ingest\n\nGrounded body.\n", encoding="utf-8")
    original_write = storage.atomic_write
    failed = False

    def fail_once(path: Path, data: bytes):
        nonlocal failed
        if not failed and path.name == "log.md":
            failed = True
            raise OSError("injected ingest failure")
        return original_write(path, data)

    monkeypatch.setattr(storage, "atomic_write", fail_once)
    with pytest.raises(OSError, match="injected ingest failure"):
        service.ingest_commit(
            "raw/local/retry-ingest.md", "new", title="Retry Ingest",
            category={"kind": "create", "name": "Retry Category"}, tags=["retry"],
        )
    base = f"ingest-{service.ingest_preview('raw/local/retry-ingest.md')['raw']['byte_hash'][:20]}-new"
    assert service.files.operation(base)["status"] == "rolled_back"
    assert not (service.root / "wiki" / "Retry-Category").exists()
    assert not any(row["path"] == "raw/local/retry-ingest.md" for row in service.state.raw_records())

    result = service.ingest_commit(
        "raw/local/retry-ingest.md", "new", title="Retry Ingest",
        category={"kind": "create", "name": "Retry Category"}, tags=["retry"],
    )
    assert result["operation_id"] == f"{base}-attempt-2"
    assert result["article"]["path"] == "Retry-Category/Retry-Ingest.md"


def test_ingest_pre_manifest_crash_retries_with_next_attempt(
    service: WikiService, monkeypatch: pytest.MonkeyPatch,
):
    raw = service.root / "raw" / "local" / "orphan-operation.md"
    raw.write_text("# Orphan Operation\n\nGrounded body.\n", encoding="utf-8")
    preview = service.ingest_preview("raw/local/orphan-operation.md")
    base = f"ingest-{preview['raw']['byte_hash'][:20]}-seed"
    orphan = service.files.history_root / base
    original_write_manifest = service.files._write_manifest
    failed = False

    def fail_before_manifest(directory: Path, manifest: dict):
        nonlocal failed
        if not failed and directory.name == base:
            failed = True
            raise OSError("injected pre-manifest crash")
        return original_write_manifest(directory, manifest)

    monkeypatch.setattr(service.files, "_write_manifest", fail_before_manifest)
    with pytest.raises(OSError, match="pre-manifest"):
        service.ingest_commit(
            "raw/local/orphan-operation.md", "seed", title="Orphan Operation",
        )
    assert (orphan / "before").is_dir()
    assert not (orphan / "manifest.json").exists()
    assert not (service.root / "wiki" / "_inbox" / "Orphan-Operation.md").exists()
    monkeypatch.setattr(service.files, "_write_manifest", original_write_manifest)

    result = service.ingest_commit(
        "raw/local/orphan-operation.md", "seed", title="Orphan Operation",
    )

    assert result["operation_id"] == f"{base}-attempt-2"
    assert result["article"]["path"] == "_inbox/Orphan-Operation.md"
    assert not (orphan / "manifest.json").exists()
    assert service.files.operation(f"{base}-attempt-2")["status"] == "committed"
    record = next(row for row in service.state.raw_records() if row["path"] == "raw/local/orphan-operation.md")
    assert record["operation_id"] == f"{base}-attempt-2"


def test_inline_create_does_not_adopt_unregistered_directory(service: WikiService):
    external = service.root / "wiki" / "External-Knowledge"
    external.mkdir()
    (external / "note.md").write_text("# External Note\n\nPending reconciliation.\n", encoding="utf-8")
    before = (service.root / dc.REGISTRY_REL).read_bytes()

    with pytest.raises(ValueError, match="reconcile it first"):
        service.generate("Managed Note", category={"kind": "create", "name": "External Knowledge"})

    assert (service.root / dc.REGISTRY_REL).read_bytes() == before
    assert any(item["kind"] == "new_directory" and item["payload"]["path"] == "External-Knowledge" for item in service.scan_reconciliation()["items"])


def test_old_ai_classification_methods_are_absent(service: WikiService):
    for name in (
        "enqueue_classification",
        "classification_workbench",
        "classification_preview",
        "retry_classification",
    ):
        assert not hasattr(service, name)
    with pytest.raises(ValueError, match="invalid category action"):
        service.category_preview("create", name="Hidden Create")
    for name in (
        "finalize_classification_success",
        "finalize_raw_classification_success",
        "save_classification_suggestion",
        "save_raw_classification_plan",
        "classification_suggestion",
        "raw_classification_plan",
        "classification_draft",
        "save_classification_draft",
        "clear_classification_draft",
    ):
        assert not hasattr(service.state, name)


def test_old_classification_tasks_are_retired_without_worker_call(kb_root: Path):
    state = StateStore(kb_root)
    created = now_iso()
    task_id = "f" * 32
    with state.connect() as db:
        db.execute(
            """INSERT INTO tasks(id,kind,subject,active_key,status,payload_json,attempts,next_run_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (task_id, "article-classification", "legacy", "article-classification:legacy", "queued", '{"path":"_inbox/legacy.md"}', 0, created, created, created),
        )
    reopened = StateStore(kb_root)
    try:
        retired = reopened.get_task(task_id)
        assert retired["status"] == "failed"
        assert retired["error_type"] == "feature_removed"
        assert reopened.claim_task({"article-classification", "raw-classification-plan"}) is None
        with pytest.raises(ValueError, match="no longer supported"):
            reopened.retry_task(task_id)
        with pytest.raises(ValueError, match="no longer supported"):
            reopened.enqueue_task("raw-classification-plan", "legacy", {})
        service = WikiService(kb_root, start_worker=False)
        try:
            with pytest.raises(ValueError, match="no longer supported"):
                service.retry_task(task_id)
            assert service.state.get_task(task_id)["status"] == "failed"
        finally:
            service.close()
    finally:
        pass


def test_category_archive_restore_and_nonempty_delete(service: WikiService):
    category = next(item for item in service.categories() if item["directory_name"] == "concepts")
    service.category_commit(service.category_preview("archive", category_id=category["category_id"])["preview_id"])
    assert category["category_id"] not in {item["id"] for item in service.taxonomy()["categories"]}
    service.category_commit(service.category_preview("restore", category_id=category["category_id"])["preview_id"])
    assert category["category_id"] in {item["id"] for item in service.taxonomy()["categories"]}
    deletion = service.category_preview("delete", category_id=category["category_id"])
    assert deletion["can_commit"] is False
    assert "directory_not_empty" in {item["kind"] for item in deletion["conflicts"]}


def test_adopt_external_directory_remains_explicit(service: WikiService):
    external = service.root / "wiki" / "External"
    external.mkdir()
    (external / "note.md").write_text("# External Note\n\nBody\n", encoding="utf-8")
    item = next(row for row in service.scan_reconciliation()["items"] if row["kind"] == "new_directory")
    preview = service.reconciliation_preview(item["id"], "adopt")
    result = service.reconciliation_commit(preview["preview_id"])
    article = service.read_article("External/note.md")
    assert result["operation_id"]
    assert article["primary_category_id"]


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
    assert {item["directory_name"] for item in service.categories()} >= {"ExternalA", "ExternalB"}


def test_concurrent_inline_creation_reuses_normalized_category_and_tag(service: WikiService):
    def create(title: str, category: str, tag: str):
        return service.generate(
            title,
            category={"kind": "create", "name": category},
            tags=[tag],
        )["article"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(
            lambda args: create(*args),
            (("Concurrent One", "ＡＩ", "Ｃｏｄｅｘ"), ("Concurrent Two", "ai", "codex")),
        ))
    assert first["primary_category_id"] == second["primary_category_id"]
    assert first["tags"] == second["tags"] == ["Codex"]
    assert len([item for item in service.categories() if dc.normalized_key(item["name"]) == "ai"]) == 1
