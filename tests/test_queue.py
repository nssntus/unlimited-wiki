from __future__ import annotations

import threading
from pathlib import Path

from state_store import StateStore
from wiki_service import WikiService


def test_running_task_recovers_to_queue_after_restart(kb_root: Path):
    state = StateStore(kb_root)
    task, created = state.enqueue_task("supplement", "Restart", {"path": "concepts/base.md"})
    assert created
    claimed = state.claim_task()
    assert claimed and claimed["status"] == "running"
    restarted = StateStore(kb_root)
    assert restarted.get_task(task["id"])["status"] == "queued"


def test_cancelled_running_task_cannot_be_completed(kb_root: Path):
    state = StateStore(kb_root)
    task, _ = state.enqueue_task("supplement", "Cancel", {})
    state.claim_task()
    state.cancel_task(task["id"])
    result = state.complete_task(task["id"], {"should": "not win"})
    assert result["status"] == "cancelled"


def test_concurrent_enqueue_has_one_active_task(kb_root: Path):
    state = StateStore(kb_root)
    barrier = threading.Barrier(8)
    task_ids: list[str] = []

    def enqueue() -> None:
        barrier.wait()
        task, _ = state.enqueue_task("supplement", "Same Subject", {})
        task_ids.append(task["id"])

    threads = [threading.Thread(target=enqueue) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(task_ids)) == 1
    assert len([task for task in state.list_tasks() if task["status"] in {"queued", "running"}]) == 1


def test_worker_claim_can_be_limited_to_user_generation_tasks(kb_root: Path):
    state = StateStore(kb_root)
    governance, _ = state.enqueue_task("governance", "Whole Wiki", {"path": "concepts/base.md"})
    generated, _ = state.enqueue_task("generate", "Clicked Term", {"path": "concepts/base.md"})

    claimed = state.claim_task({"generate", "supplement"})

    assert claimed and claimed["id"] == generated["id"]
    assert state.get_task(governance["id"])["status"] == "queued"


def test_remote_result_never_overwrites_edited_draft(kb_root: Path):
    gate = threading.Event()

    def remote(*_args, **_kwargs):
        gate.wait(timeout=2)
        return [{"title": "Source", "url": "https://example.com/source", "text": "Conflict term 是一个经过补证的概念。" * 20, "published": None}]

    service = WikiService(kb_root, remote_search=remote, start_worker=True)
    try:
        generated = service.generate("Conflict term")
        article = generated["article"]
        edited = article["markdown"] + "\n人工编辑。\n"
        service.save_article(article["path"], edited, article["revision"])
        gate.set()
        task_id = generated["task"]["id"]
        task = service.state.get_task(task_id)
        for _ in range(100):
            task = service.state.get_task(task_id)
            if task["status"] in {"succeeded", "failed"}:
                break
            threading.Event().wait(0.02)
        assert task["status"] == "succeeded"
        assert task["result"]["conflict"] is True
        assert service.read_article(article["path"])["markdown"].endswith("人工编辑。\n")
    finally:
        service.close()


def test_conflicted_task_can_retry_from_current_revision(kb_root: Path):
    state = StateStore(kb_root)
    task, _ = state.enqueue_task("supplement", "Conflict", {"path": "concepts/base.md", "base_revision": "old"})
    state.claim_task()
    state.complete_task(task["id"], {"conflict": True, "reason": "article_changed"})
    retried = state.retry_task(task["id"], payload={"path": "concepts/base.md", "base_revision": "current"})
    assert retried["status"] == "queued"
    assert retried["result"] is None
    assert retried["payload"]["base_revision"] == "current"


def test_concurrent_retry_only_one_request_requeues_task(kb_root: Path):
    state = StateStore(kb_root)
    task, _ = state.enqueue_task("supplement", "Retry once", {"path": "concepts/base.md"})
    claimed = state.claim_task()
    assert claimed and claimed["id"] == task["id"]
    state.fail_task(task["id"], "model_error", "failed", retry=False, expected_attempt=claimed["attempts"])
    barrier = threading.Barrier(2)
    retried: list[dict] = []
    errors: list[Exception] = []

    def retry() -> None:
        barrier.wait()
        try:
            retried.append(state.retry_task(task["id"]))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=retry) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert len(retried) == 1
    assert retried[0]["status"] == "queued"
    assert len(errors) == 1 and str(errors[0]) == "task cannot be retried"
    assert state.get_task(task["id"])["status"] == "queued"
