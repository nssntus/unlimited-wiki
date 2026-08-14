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
