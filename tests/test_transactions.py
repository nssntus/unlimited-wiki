from __future__ import annotations

from pathlib import Path

import pytest

import storage
from storage import FileStore


def test_commit_and_manual_rollback(kb_root: Path):
    store = FileStore(kb_root)
    before = (kb_root / "wiki" / "concepts" / "base.md").read_bytes()
    manifest = store.commit(
        {"wiki/concepts/base.md": "changed", "wiki/concepts/new.md": "new"},
        kind="test",
        operation_id="test-rollback",
    )
    assert manifest["status"] == "committed"
    store.rollback("test-rollback")
    assert (kb_root / "wiki" / "concepts" / "base.md").read_bytes() == before
    assert not (kb_root / "wiki" / "concepts" / "new.md").exists()


def test_create_only_commit_does_not_overwrite_existing_file(kb_root: Path):
    store = FileStore(kb_root)
    path = kb_root / "raw" / "local" / "existing.txt"
    path.write_text("original", encoding="utf-8")
    with pytest.raises(FileExistsError):
        store.commit(
            {"raw/local/existing.txt": "replacement"},
            kind="raw-upload",
            operation_id="create-only",
            must_not_exist=True,
        )
    assert path.read_text(encoding="utf-8") == "original"
    assert not (store.history_root / "create-only").exists()


def test_commit_distinguishes_target_and_operation_slot_conflicts(kb_root: Path):
    store = FileStore(kb_root)
    existing = kb_root / "wiki" / "concepts" / "base.md"
    with pytest.raises(storage.TransactionTargetExistsError):
        store.commit(
            {"wiki/concepts/base.md": "replacement"},
            kind="test",
            operation_id="target-conflict",
            must_not_exist_paths={"wiki/concepts/base.md"},
        )
    assert existing.exists()

    (store.history_root / "operation-conflict" / "before").mkdir(parents=True)
    with pytest.raises(storage.OperationExistsError):
        store.commit(
            {"wiki/concepts/new.md": "new"},
            kind="test",
            operation_id="operation-conflict",
        )


def test_mid_commit_failure_restores_before_images(kb_root: Path, monkeypatch: pytest.MonkeyPatch):
    store = FileStore(kb_root)
    before = (kb_root / "wiki" / "concepts" / "base.md").read_bytes()
    original = storage.atomic_write
    failed = False

    def fail_once(path: Path, data: bytes):
        nonlocal failed
        if path.name == "new.md" and not failed:
            failed = True
            raise OSError("injected failure")
        return original(path, data)

    monkeypatch.setattr(storage, "atomic_write", fail_once)
    with pytest.raises(OSError):
        store.commit(
            {"wiki/concepts/base.md": "changed", "wiki/concepts/new.md": "new"},
            kind="test",
            operation_id="test-failure",
        )
    assert (kb_root / "wiki" / "concepts" / "base.md").read_bytes() == before
    assert not (kb_root / "wiki" / "concepts" / "new.md").exists()
    assert store.operation("test-failure")["status"] == "rolled_back"
