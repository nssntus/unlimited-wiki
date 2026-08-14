from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from wiki_service import WikiService
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


def test_raw_ingest_keeps_source_immutable_and_is_duplicate_safe(service: WikiService, kb_root: Path):
    raw_path = kb_root / "raw" / "local" / "sample.txt"
    raw_path.write_text("Raw Definition 是一个至少八十个字符的明确释义。" * 8, encoding="utf-8")
    before = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    result = service.ingest_commit("raw/local/sample.txt", "new", title="Raw Definition", category="concepts")
    assert result["target_path"] == "concepts/Raw-Definition.md"
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
        result = service.ingest_commit("raw/local/seed.md", "seed", title="Seed Wiki", category="ai-practice")
        article = result["article"]
        assert "## 第一部分\n\n原始正文。" in article["markdown"]
        assert "## 第二部分\n\n更多原始正文。" in article["markdown"]
        assert article["generation"].startswith("seed-adopted")
        assert article["completeness"] == "完整"
        assert result["task"] is None
        assert service.state.list_tasks() == []
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
