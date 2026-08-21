from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import dynamic_categories as dc
from wiki_service import RemoteTaskUnavailable, WikiService
from wiki_service import LLMConfig, extract_markdown_article, model_message_text


@pytest.fixture
def service(kb_root: Path):
    instance = WikiService(kb_root, start_worker=False)
    try:
        yield instance
    finally:
        instance.close()


def test_status_and_structure_are_independent(service: WikiService):
    result = service.apply_meta("concepts/base.md", category="tools", status="有争议")
    article = result["article"]
    assert article["content_status"] == "有争议"
    assert article["completeness"] == "完整"
    assert service.read_article("concepts/base.md")["path"] == article["path"]
    assert len(service.articles()) == 2


def test_apply_meta_notifies_platform_path_projection(service: WikiService):
    remaps: list[list[dict[str, str]]] = []
    service.path_remap_callback = lambda value: remaps.append(value)
    result = service.apply_meta("concepts/base.md", category="tools", status="词条")
    assert result["article"]["path"] == "tools/base.md"
    assert remaps == [[{
        "article_id": result["article"]["article_id"],
        "private_path": "tools/base.md",
        "article_revision": result["article"]["revision"],
    }]]


def test_apply_meta_recovers_state_and_platform_path_projection_after_commit_crash(
    kb_root: Path, monkeypatch: pytest.MonkeyPatch,
):
    first = WikiService(kb_root, start_worker=False)
    first.state.record_raw(
        "raw/local/base.txt", "byte-hash", "text-hash", "imported",
        "concepts/base.md", "seed-operation",
    )
    monkeypatch.setattr(
        first,
        "_remap_committed_paths",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("crash after commit")),
    )
    with pytest.raises(SystemExit, match="crash after commit"):
        first.apply_meta("concepts/base.md", category="tools", status="词条")
    committed = next(
        manifest for path in first.files.history_root.glob("meta-*/manifest.json")
        if (manifest := first.files.operation(path.parent.name))["status"] == "committed"
    )
    assert committed["metadata"]["path_map"] == {"concepts/base.md": "tools/base.md"}
    assert first.state.raw_records()[0]["target_path"] == "concepts/base.md"
    first.close()

    remaps: list[list[dict[str, str]]] = []
    restarted = WikiService(kb_root, start_worker=False, path_remap_callback=remaps.append)
    try:
        assert restarted.state.raw_records()[0]["target_path"] == "tools/base.md"
        assert remaps[0][0]["private_path"] == "tools/base.md"
        assert restarted.read_article("tools/base.md")["title"] == "Base"
    finally:
        restarted.close()


def test_apply_meta_recovery_resolves_reverse_ordered_move_chain_to_current_article(
    kb_root: Path, monkeypatch: pytest.MonkeyPatch,
):
    first = WikiService(kb_root, start_worker=False)
    first.state.record_raw(
        "raw/local/base.txt", "byte-hash", "text-hash", "imported",
        "concepts/base.md", "seed-operation",
    )
    monkeypatch.setattr(
        first,
        "_remap_committed_paths",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("crash after commit")),
    )
    with pytest.raises(SystemExit):
        first.apply_meta("concepts/base.md", category="tools", status="词条")
    with pytest.raises(SystemExit):
        first.apply_meta("tools/base.md", category="concepts", status="词条")
    manifests = []
    for path in first.files.history_root.glob("meta-*/manifest.json"):
        manifest = first.files.operation(path.parent.name)
        if manifest["status"] == "committed":
            manifests.append((path.parent, manifest))
    assert len(manifests) == 2
    first_move = next(item for item in manifests if item[1]["metadata"]["source"] == "concepts/base.md")
    second_move = next(item for item in manifests if item[1]["metadata"]["source"] == "tools/base.md")
    first_move[0].rename(first.files.history_root / "z-first")
    second_move[0].rename(first.files.history_root / "a-second")
    current_path = second_move[1]["metadata"]["target"]
    first.close()

    platform_path = {"value": "concepts/base.md"}

    def remap_platform(projections: list[dict[str, str]]) -> None:
        platform_path["value"] = projections[0]["private_path"]

    restarted = WikiService(kb_root, start_worker=False, path_remap_callback=remap_platform)
    try:
        assert restarted.state.raw_records()[0]["target_path"] == current_path
        assert platform_path["value"] == current_path
        assert restarted.resolve_article_id(restarted.read_article(current_path)["article_id"])["path"] == current_path
    finally:
        restarted.close()


def test_apply_meta_generated_article_id_recovers_and_duplicate_later_fails_closed(
    kb_root: Path, monkeypatch: pytest.MonkeyPatch,
):
    first = WikiService(kb_root, start_worker=False)
    source = first.root / "wiki" / "concepts" / "base.md"
    source.write_text(
        "".join(line for line in source.read_text(encoding="utf-8").splitlines(keepends=True)
                if not line.startswith("> Article-ID:")),
        encoding="utf-8",
    )
    first.state.record_raw(
        "raw/local/base.txt", "byte-hash", "text-hash", "imported",
        "concepts/base.md", "seed-operation",
    )
    monkeypatch.setattr(
        first,
        "_remap_committed_paths",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("crash after commit")),
    )
    with pytest.raises(SystemExit):
        first.apply_meta("concepts/base.md", category="tools", status="词条")
    manifest = next(
        first.files.operation(path.parent.name)
        for path in first.files.history_root.glob("meta-*/manifest.json")
    )
    generated_id = manifest["metadata"]["article_id"]
    assert generated_id == first.read_article("tools/base.md")["article_id"]
    assert len(generated_id) == 32
    int(generated_id, 16)
    first.close()

    remaps: list[list[dict[str, str]]] = []
    restarted = WikiService(kb_root, start_worker=False, path_remap_callback=remaps.append)
    try:
        assert restarted.state.raw_records()[0]["target_path"] == "tools/base.md"
        assert remaps[0][0]["private_path"] == "tools/base.md"
        restarted.state.remap_article_path("tools/base.md", "concepts/base.md")
        duplicate = restarted.root / "wiki" / "tools" / "duplicate.md"
        duplicate.write_bytes((restarted.root / "wiki" / "tools" / "base.md").read_bytes())
    finally:
        restarted.close()

    remaps.clear()
    ambiguous = WikiService(kb_root, start_worker=False, path_remap_callback=remaps.append)
    try:
        assert ambiguous.state.raw_records()[0]["target_path"] == "concepts/base.md"
        assert remaps == []
    finally:
        ambiguous.close()


def test_apply_meta_callback_failure_can_retry_with_a_new_operation(
    service: WikiService,
):
    calls = 0

    def fail_once(_projections: list[dict[str, str]]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("projection unavailable")

    service.path_remap_callback = fail_once
    with pytest.raises(RuntimeError, match="projection unavailable"):
        service.apply_meta("concepts/base.md", category="tools", status="词条")
    assert service.read_article("concepts/base.md")["title"] == "Base"
    assert not (service.root / "wiki" / "tools" / "base.md").exists()

    retried = service.apply_meta("concepts/base.md", category="tools", status="词条")

    assert retried["operation_id"].endswith("-attempt-2")
    assert service.files.operation(retried["operation_id"])["status"] == "committed"
    assert retried["article"]["path"] == "tools/base.md"
    assert calls == 2


def test_legacy_apply_meta_retry_keeps_operation_base_when_generated_id_changes(
    service: WikiService, monkeypatch: pytest.MonkeyPatch,
):
    source = service.root / "wiki" / "concepts" / "base.md"
    source.write_text(
        "".join(line for line in source.read_text(encoding="utf-8").splitlines(keepends=True)
                if not line.startswith("> Article-ID:")),
        encoding="utf-8",
    )
    timestamps = iter(("2026-08-17T00:00:00+00:00", "2026-08-17T00:00:02+00:00"))
    monkeypatch.setattr(dc, "now_iso", lambda: next(timestamps))
    calls = 0

    def fail_once(_projections: list[dict[str, str]]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("projection unavailable")

    service.path_remap_callback = fail_once
    with pytest.raises(RuntimeError, match="projection unavailable"):
        service.apply_meta("concepts/base.md", category="tools", status="词条")
    rolled_back = next(
        service.files.operation(path.parent.name)
        for path in service.files.history_root.glob("meta-*/manifest.json")
    )

    retried = service.apply_meta("concepts/base.md", category="tools", status="词条")
    committed = service.files.operation(retried["operation_id"])

    assert rolled_back["status"] == "rolled_back"
    assert retried["operation_id"] == f"{rolled_back['operation_id']}-attempt-2"
    assert rolled_back["metadata"]["article_id"] != committed["metadata"]["article_id"]
    assert committed["metadata"]["article_id"] == retried["article"]["article_id"]


def test_model_markdown_extractor_accepts_preamble_and_fence():
    response = "下面是词条：\n\n```markdown\n# Prompt Engineering\n\n正文。\n```\n"
    assert extract_markdown_article(response) == "# Prompt Engineering\n\n正文。\n"


def test_model_message_text_accepts_multipart_content():
    message = SimpleNamespace(content=[{"type": "text", "text": "# Multipart"}, SimpleNamespace(text="正文。")])
    assert model_message_text(message) == "# Multipart\n正文。"


def test_model_markdown_extractor_adds_missing_h1_to_sectioned_article():
    response = "以下是正文：\n\n## Overview\n\n正文。\n\n## 例子\n\n示例。"
    assert extract_markdown_article(response, fallback_title="Fallback") == "# Fallback\n\n以下是正文：\n\n## Overview\n\n正文。\n\n## 例子\n\n示例。\n"


def test_model_message_text_ignores_thinking_without_article():
    message = SimpleNamespace(content="", reasoning_content="先分析要求，再撰写正文。")
    assert model_message_text(message) == ""


def test_model_markdown_completion_repairs_first_invalid_response(service: WikiService):
    calls: list[dict] = []
    responses = iter(["我会为你生成词条。", "# Corrected\n\n正文。"])

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=next(responses)))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = service._complete_markdown(client, "生成词条", "Corrected")

    assert result == "# Corrected\n\n正文。\n"
    assert len(calls) == 2
    assert "严格重写" in calls[1]["messages"][-1]["content"]
    assert all(message["role"] != "assistant" for message in calls[1]["messages"])


def test_generate_is_local_first_and_creates_one_canonical(service: WikiService):
    first = service.generate("Base")
    assert first["created"] is False
    second = service.generate("隔离概念", from_path="concepts/base.md")
    assert second["created"] is True
    assert second["article"]["evidence_status"] == "待补证"
    assert "[隔离概念]" in service.read_article("concepts/base.md")["markdown"]
    replay = service.generate("隔离概念")
    assert replay["created"] is False
    assert [item["title"] for item in service.articles()].count("隔离概念") == 1


def test_disabled_remote_tasks_reject_before_generate_or_ingest_writes(kb_root: Path, snapshot, monkeypatch: pytest.MonkeyPatch):
    service = WikiService(
        kb_root,
        llm_config=LLMConfig(base_url="https://models.example/v1", model="test"),
        start_worker=False,
        remote_tasks_enabled=False,
    )
    raw_path = kb_root / "raw" / "local" / "disabled.txt"
    raw_path.write_text("# Disabled\n\nEnough local source text for an ingest test.", encoding="utf-8")
    before = snapshot(kb_root)
    try:
        with pytest.raises(RemoteTaskUnavailable) as generated:
            service.generate("Worker disabled concept")
        assert (generated.value.kind, generated.value.reason) == ("supplement", "disabled")
        assert service.state.task_counts()["queued"] == 0
        assert snapshot(kb_root) == before

        with pytest.raises(RemoteTaskUnavailable) as ingested:
            service.ingest_commit("raw/local/disabled.txt", "new", title="Disabled ingest")
        assert (ingested.value.kind, ingested.value.reason) == ("governance", "disabled")
        assert service.state.raw_records() == []
        assert snapshot(kb_root) == before

        preflight = {
            "keyword": "Local only",
            "existing_path": None,
            "local_coverage": {"sufficient": True},
            "context": {"from_path": "", "heading": "", "passage": ""},
            "excerpts": [],
            "plan": "local_generate",
        }
        service.configure_llm(LLMConfig())
        monkeypatch.setattr(service, "preflight_generate", lambda *args, **kwargs: preflight)
        local = service.generate("Local only")
        assert local["created"] is True and local["task"] is None
    finally:
        service.close()


def test_kind_disabled_rejects_governance_and_retry_without_mutation(kb_root: Path):
    service = WikiService(
        kb_root,
        llm_config=LLMConfig(base_url="https://models.example/v1", model="test"),
        start_worker=False,
        remote_task_kinds={"generate", "supplement"},
    )
    try:
        with pytest.raises(RemoteTaskUnavailable) as governance:
            service.enqueue_governance()
        assert (governance.value.kind, governance.value.reason) == ("governance", "kind_disabled")
        task, _ = service.state.enqueue_task("governance", "Base", {"path": "concepts/base.md"})
        claimed = service.state.claim_task({"governance"})
        service.state.fail_task(claimed["id"], "model_error", "failed", retry=False)
        before = service.state.get_task(task["id"])
        with pytest.raises(RemoteTaskUnavailable):
            service.retry_task(task["id"])
        assert service.state.get_task(task["id"]) == before
    finally:
        service.close()


def test_raw_ingest_keeps_source_immutable_and_is_duplicate_safe(service: WikiService, kb_root: Path):
    raw_path = kb_root / "raw" / "local" / "sample.txt"
    raw_path.write_text("Raw Definition 是一个至少八十个字符的明确释义。" * 8, encoding="utf-8")
    before = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    result = service.ingest_commit("raw/local/sample.txt", "new", title="Raw Definition", category="concepts")
    assert result["target_path"] == "concepts/Raw-Definition.md"
    assert result["article"]["classification_status"] == "confirmed"
    assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == before
    copy = kb_root / "raw" / "local" / "sample-copy.txt"
    copy.write_bytes(raw_path.read_bytes())
    duplicate = next(item for item in service.raw_inbox() if item["path"].endswith("sample-copy.txt"))
    assert duplicate["status"] == "duplicate"
    service.ingest_commit(duplicate["path"], "duplicate", title="Raw Definition", category="concepts", target_path=result["target_path"])
    assert [item["title"] for item in service.articles()].count("Raw Definition") == 1


def test_category_move_remaps_raw_record_to_canonical_path(service: WikiService, kb_root: Path):
    raw_path = kb_root / "raw" / "local" / "move-me.txt"
    raw_path.write_text("# Move Me\n\nA grounded definition with enough local context.", encoding="utf-8")
    ingested = service.ingest_commit("raw/local/move-me.txt", "new", title="Move Me", category="concepts")

    moved = service.apply_meta(ingested["target_path"], category="tools", status="词条")

    inbox_item = next(item for item in service.raw_inbox() if item["path"] == "raw/local/move-me.txt")
    record = next(item for item in service.state.raw_records() if item["path"] == "raw/local/move-me.txt")
    assert inbox_item["linked_target"] == moved["article"]["path"]
    assert record["target_path"] == moved["article"]["path"]


def test_raw_upload_adds_material_and_versions_same_filename(service: WikiService):
    first = service.upload_raw("notes.md", "# Notes\n\n第一版材料。")
    replay = service.upload_raw("copy.md", "# Notes\r\n\r\n第一版材料。")
    second = service.upload_raw("notes.md", "# Notes\n\n第二版材料。")
    assert first["created"] is True
    assert first["raw"]["path"] == "raw/local/notes.md"
    assert replay["created"] is False
    assert replay["raw"]["path"] == first["raw"]["path"]
    assert second["created"] is True
    assert second["raw"]["path"] == "raw/local/notes-2.md"


def test_raw_upload_after_rollback_uses_a_new_operation(service: WikiService, monkeypatch: pytest.MonkeyPatch):
    commit_calls = []
    original_commit = service.files.commit

    def bounded_commit(*args, **kwargs):
        commit_calls.append(kwargs.get("operation_id"))
        if len(commit_calls) > 3:
            raise AssertionError("Raw upload retried indefinitely")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(service.files, "commit", bounded_commit)
    content = "# Rollback Raw\n\n允许回滚后重新上传的原材料。"
    first = service.upload_raw("rollback.md", content)
    service.rollback(first["operation_id"])

    second = service.upload_raw("rollback.md", content)

    assert second["created"] is True
    assert second["raw"]["path"] == "raw/local/rollback.md"
    assert second["operation_id"] != first["operation_id"]
    assert service.files.operation(first["operation_id"])["status"] == "rolled_back"
    assert service.files.operation(second["operation_id"])["status"] == "committed"
    assert commit_calls == [first["operation_id"], second["operation_id"]]


def test_raw_filename_retry_keeps_one_operation_id(service: WikiService, monkeypatch: pytest.MonkeyPatch):
    (service.root / "raw" / "local" / "collision.md").write_text("# Existing\n\n已有原材料。", encoding="utf-8")
    operation_ids = []
    original_commit = service.files.commit

    def recording_commit(*args, **kwargs):
        operation_ids.append(kwargs.get("operation_id"))
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(service.files, "commit", recording_commit)
    result = service.upload_raw("collision.md", "# New\n\n同名但内容不同的新原材料。")

    assert result["raw"]["path"] == "raw/local/collision-2.md"
    assert len(operation_ids) == 2
    assert set(operation_ids) == {result["operation_id"]}


def test_concurrent_same_name_raw_uploads_never_overwrite(service: WikiService, monkeypatch: pytest.MonkeyPatch):
    barrier = threading.Barrier(2)
    local = threading.local()
    original = service.raw_inbox

    def synchronized_inbox():
        result = original()
        if not getattr(local, "waited", False):
            local.waited = True
            barrier.wait(timeout=3)
        return result

    monkeypatch.setattr(service, "raw_inbox", synchronized_inbox)
    contents = ["# Concurrent\n\n第一份不可变原料。", "# Concurrent\n\n第二份不可变原料。"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda value: service.upload_raw("concurrent.md", value), contents))

    paths = {result["raw"]["path"] for result in results}
    assert paths == {"raw/local/concurrent.md", "raw/local/concurrent-2.md"}
    assert {(service.root / path).read_text(encoding="utf-8") for path in paths} == set(contents)


def test_seed_ingest_preserves_complete_source_without_ai_rewrite(kb_root: Path):
    raw = "# Seed Wiki\n\n## 第一部分\n\n原始正文。\n\n## 第二部分\n\n更多原始正文。\n"
    raw_path = kb_root / "raw" / "local" / "seed.md"
    raw_path.write_text(raw, encoding="utf-8")
    service = WikiService(
        kb_root,
        llm_config=LLMConfig(base_url="http://127.0.0.1:9999/v1", model="test"),
        start_worker=False,
    )
    try:
        result = service.ingest_commit("raw/local/seed.md", "seed", title="Seed Wiki", category="concepts")
        article = result["article"]
        assert "## 第一部分\n\n原始正文。" in article["markdown"]
        assert "## 第二部分\n\n更多原始正文。" in article["markdown"]
        assert article["generation"].startswith("seed-adopted")
        assert article["completeness"] == "完整"
        assert result["task"] is None
        assert "classification_task" not in result
        assert raw_path.read_text(encoding="utf-8") == raw
        assert __import__("wiki_ops").article_quality_issues(kb_root, article["path"], article["markdown"]) == []
    finally:
        service.close()


def test_seed_keywords_are_highlightable_without_cross_document_mentions(kb_root: Path):
    seed = kb_root / "wiki" / "ai-practice" / "seed.md"
    seed.parent.mkdir(exist_ok=True)
    seed.write_text(
        "# Seed\n\n> Category: ai-practice\n> Status: 词条\n> Raw: [Seed](../../raw/local/seed.md)\n"
        "> Generation: seed-adopted; source-preserved=true\n\n## Context Engineering\n\n**Prompt Engineering** 是核心能力。\n",
        encoding="utf-8",
    )
    terms = {item["term"] for item in __import__("wiki_ops").highlightable_keywords(kb_root) if item["kind"] == "missing"}
    assert "Context Engineering" in terms
    assert "Prompt Engineering" in terms


@pytest.mark.parametrize("filename", ["../secret.md", "image.png", ".hidden.md", "", "/tmp/file.md"])
def test_raw_upload_rejects_unsafe_filename(service: WikiService, filename: str):
    with pytest.raises(ValueError):
        service.upload_raw(filename, "材料")


def test_raw_inbox_recovers_linked_state_from_wiki(service: WikiService, kb_root: Path):
    raw_path = kb_root / "raw" / "local" / "recovery.txt"
    raw_path.write_text("# Recovery\n\n不可变 Raw 内容。", encoding="utf-8")
    article_path = kb_root / "wiki" / "concepts" / "base.md"
    article_path.write_text(
        article_path.read_text(encoding="utf-8") + "\n> Raw: [Recovery](../../raw/local/recovery.txt)\n",
        encoding="utf-8",
    )
    item = next(row for row in service.raw_inbox() if row["path"] == "raw/local/recovery.txt")
    assert item["status"] == "ingested"
    assert item["linked_target"] == "concepts/base.md"


def test_lint_reports_content_quality(service: WikiService, kb_root: Path):
    thin = kb_root / "wiki" / "concepts" / "thin.md"
    thin.write_text("# Thin\n\n> Category: concepts\n\n只有一句。\n", encoding="utf-8")
    issues = __import__("wiki_ops").lint_wiki(kb_root)
    assert any(issue["kind"] == "content_quality" and issue["path"] == "concepts/thin.md" for issue in issues)


def test_lint_rejects_free_form_content_status(service: WikiService, kb_root: Path):
    article = kb_root / "wiki" / "concepts" / "base.md"
    article.write_text(article.read_text(encoding="utf-8").replace("> Status: 词条", "> Status: Active"), encoding="utf-8")
    issues = __import__("wiki_ops").lint_wiki(kb_root)
    assert any(issue["kind"] == "content_quality" and "非法内容状态：Active" in issue["detail"] for issue in issues)


def test_ai_normalization_removes_dead_links_and_adds_known_backlinks(service: WikiService):
    markdown = """# Draft

> Category: concepts
> Status: 草稿
> Sources: [Source](source.md)

## 它做什么

Base 与 [不存在](missing.md) 共同出现。

## 怎么用

按来源使用。

## 例子

这是例子。

## See Also
"""
    normalized = service._normalize_ai_article(markdown, "concepts/draft.md", "Draft", "concepts")
    assert "missing.md" not in normalized
    assert "[Base](base.md)" in normalized
    assert __import__("wiki_ops").article_quality_issues(service.root, "concepts/draft.md", normalized) == []


def test_ai_normalization_maps_common_heading_variants(service: WikiService):
    markdown = """# Draft

> Category: concepts
> Status: 草稿
> Sources: [Source](source.md)

## 工作原理

有依据的机制说明。

## 如何使用

按来源使用。

## 示例

这是例子。
"""
    normalized = service._normalize_ai_article(markdown, "concepts/draft.md", "Draft", "concepts")
    assert "## 它做什么" in normalized
    assert "## 怎么用" in normalized
    assert "## 例子" in normalized
    assert __import__("wiki_ops").article_quality_issues(service.root, "concepts/draft.md", normalized) == []


def test_ai_governance_queues_each_actionable_article_once(kb_root: Path):
    thin = kb_root / "wiki" / "concepts" / "thin.md"
    thin.write_text("# Thin\n\n> Category: concepts\n\n只有一句。\n", encoding="utf-8")
    service = WikiService(
        kb_root,
        llm_config=LLMConfig(base_url="http://127.0.0.1:9999/v1", model="test"),
        start_worker=False,
    )
    try:
        first = service.enqueue_governance()
        second = service.enqueue_governance()
        assert first["queued"] == 2
        assert second["queued"] == 0
        assert {task["id"] for task in first["tasks"]} == {task["id"] for task in second["tasks"]}
        assert all(task["kind"] == "governance" for task in first["tasks"])
    finally:
        service.close()


def test_governance_preserves_source_metadata(service: WikiService, monkeypatch: pytest.MonkeyPatch):
    article = service.read_article("concepts/base.md")
    task, _ = service.state.enqueue_task(
        "governance",
        article["path"],
        {"path": article["path"], "base_revision": article["revision"]},
    )
    claimed = service.state.claim_task()
    assert claimed and claimed["id"] == task["id"]
    model_output = """# Base

> Category: concepts
> Status: 草稿

## 它做什么

经过治理的说明。

## 怎么用

按来源使用。

## 例子

这是例子。

## See Also
"""
    monkeypatch.setattr(service, "_call_governance_llm", lambda _article: model_output)
    result = service._run_governance_task(claimed)
    updated = service.read_article("concepts/base.md")["markdown"]
    assert result["conflict"] is False
    assert "> Sources: [Source](source.md)" in updated


def test_cancel_cannot_split_governance_commit_from_success(service: WikiService, monkeypatch: pytest.MonkeyPatch):
    article = service.read_article("concepts/base.md")
    task, _ = service.state.enqueue_task(
        "governance", article["path"], {"path": article["path"], "base_revision": article["revision"]},
    )
    claimed = service.state.claim_task({"governance"})
    assert claimed and claimed["id"] == task["id"]
    monkeypatch.setattr(service, "_call_governance_llm", lambda _article: article["markdown"].replace("这是一个例子。", "这是一个治理后的例子。"))
    monkeypatch.setattr("wiki_ops.article_quality_issues", lambda *_args, **_kwargs: [])
    committed = threading.Event()
    release_commit = threading.Event()
    cancel_done = threading.Event()
    real_commit = service.files.commit
    result: dict = {}

    def delayed_commit(*args, **kwargs):
        manifest = real_commit(*args, **kwargs)
        committed.set()
        assert release_commit.wait(3)
        return manifest

    monkeypatch.setattr(service.files, "commit", delayed_commit)
    worker = threading.Thread(target=lambda: result.update(task=service._run_governance_task(claimed)))
    worker.start()
    assert committed.wait(3)

    def cancel() -> None:
        result["cancel"] = service.cancel_task(task["id"])
        cancel_done.set()

    canceller = threading.Thread(target=cancel)
    canceller.start()
    assert not cancel_done.wait(0.1)
    release_commit.set()
    worker.join(timeout=3)
    canceller.join(timeout=3)

    assert result["task"]["operation_id"] == f"govern-{task['id']}-attempt-{claimed['attempts']}"
    assert result["cancel"]["status"] == "succeeded"
    assert service.state.get_task(task["id"])["status"] == "succeeded"
    assert "治理后的例子" in service.read_article(article["path"])["markdown"]


def test_old_retried_governance_attempt_cannot_write_files(service: WikiService, monkeypatch: pytest.MonkeyPatch):
    article = service.read_article("concepts/base.md")
    original = (service.root / "wiki" / article["path"]).read_bytes()
    task, _ = service.state.enqueue_task(
        "governance", article["path"], {"path": article["path"], "base_revision": article["revision"]},
    )
    first = service.state.claim_task({"governance"})
    assert first and first["id"] == task["id"]
    model_started = threading.Event()
    release_model = threading.Event()
    result: dict = {}

    def delayed_model(_article):
        model_started.set()
        assert release_model.wait(3)
        return article["markdown"].replace("这是一个例子。", "迟到的旧结果。")

    monkeypatch.setattr(service, "_call_governance_llm", delayed_model)
    monkeypatch.setattr("wiki_ops.article_quality_issues", lambda *_args, **_kwargs: [])
    worker = threading.Thread(target=lambda: result.update(task=service._run_governance_task(first)))
    worker.start()
    assert model_started.wait(3)
    assert service.cancel_task(task["id"])["status"] == "cancelled"
    assert service.retry_task(task["id"])["status"] == "queued"
    second = service.state.claim_task({"governance"})
    assert second and second["attempts"] == first["attempts"] + 1
    release_model.set()
    worker.join(timeout=3)

    assert result["task"]["superseded"] is True
    assert service.state.get_task(task["id"])["status"] == "running"
    assert service.state.get_task(task["id"])["attempts"] == second["attempts"]
    assert (service.root / "wiki" / article["path"]).read_bytes() == original
    assert list(service.files.history_root.glob(f"govern-{task['id']}-attempt-*")) == []


def test_governance_rolls_back_when_task_finalization_fails(service: WikiService, monkeypatch: pytest.MonkeyPatch):
    article = service.read_article("concepts/base.md")
    original = (service.root / "wiki" / article["path"]).read_bytes()
    task, _ = service.state.enqueue_task(
        "governance", article["path"], {"path": article["path"], "base_revision": article["revision"]},
    )
    claimed = service.state.claim_task({"governance"})
    assert claimed and claimed["id"] == task["id"]
    monkeypatch.setattr(service, "_call_governance_llm", lambda _article: article["markdown"].replace("这是一个例子。", "不应保留的结果。"))
    monkeypatch.setattr("wiki_ops.article_quality_issues", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(service.state, "complete_task", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")))

    with pytest.raises(RuntimeError, match="database unavailable"):
        service._run_governance_task(claimed)

    assert (service.root / "wiki" / article["path"]).read_bytes() == original
    operation_id = f"govern-{task['id']}-attempt-{claimed['attempts']}"
    assert service.files.operation(operation_id)["status"] == "rolled_back"


def test_merge_rewrites_inbound_links_and_rolls_back(service: WikiService, kb_root: Path):
    source = kb_root / "wiki" / "concepts" / "duplicate.md"
    source.write_text(
        "# Duplicate\n\n> Category: concepts\n> Status: 草稿\n> Aliases: Dupe\n\n## 它做什么\nA\n\n## 怎么用\nB\n\n## 例子\nC\n",
        encoding="utf-8",
    )
    inbound = kb_root / "wiki" / "concepts" / "inbound.md"
    inbound.write_text(
        "# Inbound\n\n> Category: concepts\n> Status: 词条\n\n## 它做什么\n[Duplicate](duplicate.md)\n\n## 怎么用\nB\n\n## 例子\nC\n",
        encoding="utf-8",
    )
    result = service.merge_commit("concepts/duplicate.md", "concepts/base.md")
    assert service.read_article("concepts/duplicate.md")["path"] == "concepts/base.md"
    assert "[Duplicate](base.md)" in inbound.read_text(encoding="utf-8")
    assert "Duplicate" not in [item["title"] for item in service.articles()]
    service.rollback(result["operation_id"])
    assert service.read_article("concepts/duplicate.md")["title"] == "Duplicate"
    assert "[Duplicate](duplicate.md)" in inbound.read_text(encoding="utf-8")
