from __future__ import annotations

import json
import os
import sqlite3
import stat
from types import SimpleNamespace
from pathlib import Path

import pytest

import backup_restore
from backup_restore import (
    InstanceLock,
    create_backup,
    instance_lock_path,
    restore_backup,
    restore_journal_path,
    verify_backup,
)
from platform_store import PlatformStore


def seed_instance(root: Path):
    platform = PlatformStore(root)
    user, _recovery = platform.register("owner@example.com", "Owner", "correct-horse-123")
    workspace = platform.workspace_root(user["workspace_root_name"])
    article = workspace / "wiki" / "concepts" / "backup.md"
    article.parent.mkdir(parents=True)
    article.write_text("# Backup\n\nPrivate body.\n", encoding="utf-8")
    raw = workspace / "raw" / "local" / "source.txt"
    raw.parent.mkdir(parents=True)
    raw.write_text("source bytes", encoding="utf-8")
    state_root = workspace / ".wiki-state"
    state_root.mkdir()
    with sqlite3.connect(state_root / "state.sqlite3") as db:
        db.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        db.execute("INSERT INTO marker VALUES('complete')")
    platform.save_model(
        user["workspace_id"], "openai-compatible", "https://models.example/v1", "secret-key", "model-a",
    )
    platform.create_session(user["id"])
    return platform, user, article, raw


def test_backup_verify_and_restore_round_trip(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _platform, user, article, raw = seed_instance(source)
    backup = tmp_path / "backup-001"

    manifest = create_backup(source, backup)
    assert manifest["schema_version"] == 1
    assert all(not item["path"].endswith(("-wal", "-shm")) for item in manifest["files"])
    assert verify_backup(backup)["files"] == manifest["files"]
    assert (backup / ".platform" / "master.key").stat().st_size == 32

    target = tmp_path / "restored"
    target.mkdir()
    restore_backup(backup, target)
    restored = PlatformStore(target)
    workspace = restored.workspace_root(user["workspace_root_name"])
    assert (workspace / article.relative_to(source / "spaces" / user["workspace_root_name"])).read_bytes() == article.read_bytes()
    assert (workspace / raw.relative_to(source / "spaces" / user["workspace_root_name"])).read_bytes() == raw.read_bytes()
    assert restored.authenticate("owner@example.com", "correct-horse-123") is not None
    assert restored.load_model(user["workspace_id"])["api_key"] == "secret-key"
    with restored.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None


def test_backup_refuses_running_instance_and_detects_tampering(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    lock = InstanceLock(instance_lock_path(source))
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="service is running"):
            create_backup(source, backup)
    finally:
        lock.release()
    create_backup(source, backup)
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    victim = backup / manifest["files"][0]["path"]
    victim.write_bytes(victim.read_bytes() + b"tampered")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        verify_backup(backup)


def test_restore_refuses_nonempty_data_target_without_modifying_it(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    target = tmp_path / "target"
    existing = target / "spaces" / "keep.txt"
    existing.parent.mkdir(parents=True)
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(RuntimeError, match="already contains"):
        restore_backup(backup, target)
    assert existing.read_text(encoding="utf-8") == "keep"
    assert not (target / ".platform").exists()


def test_backup_rejects_destination_inside_data_and_source_symlinks(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _platform, user, _article, _raw = seed_instance(source)
    with pytest.raises(RuntimeError, match="outside wiki data"):
        create_backup(source, source / "spaces" / "backup")

    link = source / "spaces" / user["workspace_root_name"] / "wiki" / "outside-link"
    link.symlink_to(tmp_path / "outside")
    with pytest.raises(RuntimeError, match="symbolic links"):
        create_backup(source, tmp_path / "backup-symlink")


def test_restore_resumes_after_interrupted_directory_install(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _platform, user, article, _raw = seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    target = tmp_path / "target"
    target.mkdir()

    real_replace = os.replace
    interrupted = False

    def interrupt_spaces(source_path, target_path):
        nonlocal interrupted
        if Path(target_path) == target / "spaces" and not interrupted:
            interrupted = True
            raise OSError("injected install interruption")
        return real_replace(source_path, target_path)

    monkeypatch.setattr(os, "replace", interrupt_spaces)
    with pytest.raises(OSError, match="injected"):
        restore_backup(backup, target)
    assert (target / ".platform").is_dir()
    assert not (target / "spaces").exists()
    assert restore_journal_path(target).is_file()

    monkeypatch.setattr(os, "replace", real_replace)
    restore_backup(backup, target)
    assert not restore_journal_path(target).exists()
    restored_article = target / "spaces" / user["workspace_root_name"] / article.relative_to(
        source / "spaces" / user["workspace_root_name"]
    )
    assert restored_article.read_bytes() == article.read_bytes()


def test_restore_uses_same_stable_instance_lock_as_service(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    target = tmp_path / "target"
    target.mkdir()
    lock = InstanceLock(instance_lock_path(target))
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="service is running"):
            restore_backup(backup, target)
    finally:
        lock.release()
    assert not (target / ".platform").exists()


def test_restore_rejects_forged_journal_paths_pending_and_tampered_stage(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    target = tmp_path / "target"
    target.mkdir()

    def interrupt_before_install(*_args, **_kwargs):
        raise OSError("injected before install")

    monkeypatch.setattr(backup_restore, "_copy_restore_directory", interrupt_before_install)
    with pytest.raises(OSError, match="injected"):
        restore_backup(backup, target)
    journal_path = restore_journal_path(target)
    original = json.loads(journal_path.read_text(encoding="utf-8"))
    stage = Path(original["stage"])
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    forged = {**original, "stage": str(outside)}
    journal_path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(RuntimeError, match="stage"):
        restore_backup(backup, target)
    assert marker.read_text(encoding="utf-8") == "keep"

    forged = {**original, "pending": ["spaces", "../outside"]}
    journal_path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(RuntimeError, match="journal is invalid"):
        restore_backup(backup, target)
    assert stage.is_dir()

    forged = {**original, "pending": ["spaces"]}
    journal_path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(RuntimeError, match="inconsistent for .platform"):
        restore_backup(backup, target)
    assert not (target / ".platform").exists()

    journal_path.write_text(json.dumps(original), encoding="utf-8")
    stage_manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    victim = stage / stage_manifest["files"][0]["path"]
    victim.write_bytes(victim.read_bytes() + b"tampered")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        restore_backup(backup, target)
    assert stage.is_dir()


def test_restore_applies_owner_to_final_data_trees_not_private_stage(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    target = tmp_path / "target"
    target.mkdir()
    chowned: list[tuple[Path, int, int]] = []

    monkeypatch.setattr(
        backup_restore.pwd, "getpwnam",
        lambda _owner: SimpleNamespace(pw_uid=1234, pw_gid=4321),
    )
    monkeypatch.setattr(
        backup_restore.grp, "getgrnam", lambda _owner: SimpleNamespace(gr_gid=5678),
    )
    monkeypatch.setattr(
        backup_restore.os, "chown",
        lambda path, uid, gid: chowned.append((Path(path), uid, gid)),
    )

    restore_backup(backup, target, owner="wiki-service")

    expected = {
        path
        for root in (target / ".platform", target / "spaces")
        for path in (root, *root.rglob("*"))
    }
    assert {path for path, _uid, _gid in chowned} == expected
    assert all((uid, gid) == (1234, 5678) for _path, uid, gid in chowned)
    assert all("restore-" not in path.as_posix() for path, _uid, _gid in chowned)
    assert all(path.stat().st_mode & stat.S_IWUSR for path in expected)


def test_restore_rejects_changing_owner_during_resume(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(
        backup_restore.pwd, "getpwnam",
        lambda _owner: SimpleNamespace(pw_uid=1234, pw_gid=4321),
    )
    monkeypatch.setattr(
        backup_restore.grp, "getgrnam", lambda _owner: SimpleNamespace(gr_gid=5678),
    )

    monkeypatch.setattr(
        backup_restore, "_copy_restore_directory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected install interruption")),
    )
    with pytest.raises(OSError, match="injected"):
        restore_backup(backup, target, owner="owner-a")
    journal_path = restore_journal_path(target)
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    stage = Path(journal["stage"])
    assert journal["owner"] == "owner-a"

    with pytest.raises(RuntimeError, match="owner does not match"):
        restore_backup(backup, target, owner="owner-b")
    assert journal_path.is_file()
    assert stage.is_dir()


def test_owner_repair_failure_keeps_resumable_journal(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    target = tmp_path / "target"
    target.mkdir()
    owner = backup_restore.pwd.getpwuid(os.getuid()).pw_name
    real_set_owner = backup_restore._set_owner
    failed = False

    def fail_once(root: Path, selected_owner: str | None):
        nonlocal failed
        if Path(root) == target / ".platform" and not failed:
            failed = True
            raise PermissionError("injected chown failure")
        return real_set_owner(root, selected_owner)

    monkeypatch.setattr(backup_restore, "_set_owner", fail_once)
    with pytest.raises(PermissionError, match="injected chown"):
        restore_backup(backup, target, owner=owner)
    journal_path = restore_journal_path(target)
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["pending"] == []
    assert Path(journal["stage"]).is_dir()
    assert (target / ".platform").is_dir()
    assert (target / "spaces").is_dir()

    monkeypatch.setattr(backup_restore, "_set_owner", real_set_owner)
    restore_backup(backup, target, owner=owner)
    assert not journal_path.exists()
    assert (target / ".platform").stat().st_uid == os.getuid()
    assert (target / "spaces").stat().st_uid == os.getuid()


def test_restore_rejects_stage_mutation_before_target_publish(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    target = tmp_path / "target"
    target.mkdir()
    real_copytree = backup_restore.shutil.copytree
    mutated_stage: list[Path] = []

    def mutate_after_install_copy(source_path, destination_path, *args, **kwargs):
        result = real_copytree(source_path, destination_path, *args, **kwargs)
        source_path = Path(source_path)
        destination_path = Path(destination_path)
        if (
            source_path.name == ".platform"
            and source_path.parent.name.startswith("restore-")
            and destination_path.name.startswith("install-.platform-")
        ):
            manifest = json.loads(
                (source_path.parent / "manifest.json").read_text(encoding="utf-8")
            )
            relative = next(
                item["path"] for item in manifest["files"]
                if item["path"].startswith(".platform/")
            )
            victim = source_path.parent / relative
            victim.write_bytes(victim.read_bytes() + b"tampered")
            mutated_stage.append(source_path.parent)
        return result

    monkeypatch.setattr(backup_restore.shutil, "copytree", mutate_after_install_copy)
    with pytest.raises(RuntimeError, match="staging changed"):
        restore_backup(backup, target)
    journal_path = restore_journal_path(target)
    assert journal_path.is_file()
    assert mutated_stage == [Path(json.loads(journal_path.read_text(encoding="utf-8"))["stage"])]
    assert mutated_stage[0].is_dir()
    assert not (target / ".platform").exists()
    assert not (target / "spaces").exists()
