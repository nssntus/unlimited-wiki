from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

import backup_restore
from backup_restore import (
    InstanceLock,
    create_backup,
    instance_lock_path,
    restore_backup,
    restore_in_progress,
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
    assert all(not item["path"].endswith((".sqlite3-wal", ".sqlite3-shm")) for item in manifest["files"])
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


def test_backup_preserves_non_sqlite_files_with_wal_and_shm_suffixes(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _platform, user, _article, _raw = seed_instance(source)
    workspace = source / "spaces" / user["workspace_root_name"]
    expected = {
        workspace / "wiki" / "ordinary-wal": b"wiki wal content",
        workspace / "raw" / "ordinary-shm": b"raw shm content",
        workspace / "wiki" / "notes.sqlite3": b"not a sqlite database",
        workspace / "wiki" / "notes.sqlite3-wal": b"ordinary attachment",
        workspace / "raw" / "manifest.json": b"user-authored manifest",
    }
    for path, content in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    backup = tmp_path / "backup-001"
    manifest = create_backup(source, backup)
    manifest_paths = {item["path"] for item in manifest["files"]}
    for path, content in expected.items():
        relative = path.relative_to(source).as_posix()
        assert relative in manifest_paths
        assert (backup / relative).read_bytes() == content

    target = tmp_path / "restored"
    restore_backup(backup, target)
    for path, content in expected.items():
        assert (target / path.relative_to(source)).read_bytes() == content


def test_backup_allows_workspace_without_initialized_state(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    platform = PlatformStore(source)
    user, _recovery = platform.register("owner@example.com", "Owner", "correct-horse-123")
    state_root = source / "spaces" / user["workspace_root_name"] / ".wiki-state"
    state_root.mkdir()

    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    assert verify_backup(backup)["schema_version"] == 1


@pytest.mark.parametrize("state_artifact", ["state.sqlite3-wal", "state.sqlite3-shm", "write.lock"])
def test_backup_rejects_initialized_workspace_without_state_database(
    tmp_path: Path, state_artifact: str,
):
    source = tmp_path / "source"
    source.mkdir()
    platform = PlatformStore(source)
    user, _recovery = platform.register("owner@example.com", "Owner", "correct-horse-123")
    state_root = source / "spaces" / user["workspace_root_name"] / ".wiki-state"
    state_root.mkdir()
    (state_root / state_artifact).write_bytes(b"orphan state artifact")

    backup = tmp_path / "backup-001"
    with pytest.raises(RuntimeError, match="SQLite database is missing"):
        create_backup(source, backup)
    assert not backup.exists()


def test_verify_rejects_orphan_workspace_sqlite_sidecar(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _platform, user, _article, _raw = seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)

    state_root = backup / "spaces" / user["workspace_root_name"] / ".wiki-state"
    database = state_root / "state.sqlite3"
    database.unlink()
    (state_root / "state.sqlite3-wal").write_bytes(b"orphan wal")
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    database_relative = database.relative_to(backup).as_posix()
    manifest["files"] = [item for item in manifest["files"] if item["path"] != database_relative]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="SQLite database is missing"):
        verify_backup(backup)


@pytest.mark.parametrize("extra_kind", ["file", "directory"])
def test_verify_rejects_entries_outside_data_directories(tmp_path: Path, extra_kind: str):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    extra = backup / "extra.txt"
    if extra_kind == "directory":
        extra.mkdir()
    else:
        extra.write_bytes(b"extra")
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"].append({
            "path": "extra.txt", "size": extra.stat().st_size, "sha256": backup_restore._sha256(extra),
        })
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="unsupported entries"):
        verify_backup(backup)


def test_verify_rejects_manifest_paths_outside_data_directories(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append({"path": "extra.txt", "size": 0, "sha256": "0" * 64})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid file entry"):
        verify_backup(backup)


@pytest.mark.parametrize("database_kind", ["platform", "workspace"])
def test_backup_and_verify_reject_corrupt_known_sqlite_databases(tmp_path: Path, database_kind: str):
    source = tmp_path / "source"
    source.mkdir()
    _platform, user, _article, _raw = seed_instance(source)

    def database(root: Path) -> Path:
        if database_kind == "platform":
            return root / ".platform" / "platform.sqlite3"
        return root / "spaces" / user["workspace_root_name"] / ".wiki-state" / "state.sqlite3"

    database(source).write_bytes(b"truncated database")
    with pytest.raises(sqlite3.DatabaseError):
        create_backup(source, tmp_path / "corrupt-source-backup")

    clean = tmp_path / "clean"
    clean.mkdir()
    _clean_platform, clean_user, _clean_article, _clean_raw = seed_instance(clean)
    user = clean_user
    backup = tmp_path / "backup-001"
    create_backup(clean, backup)
    damaged = database(backup)
    damaged.write_bytes(b"truncated backup database")
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    relative = damaged.relative_to(backup).as_posix()
    entry = next(item for item in manifest["files"] if item["path"] == relative)
    entry["size"] = damaged.stat().st_size
    entry["sha256"] = backup_restore._sha256(damaged)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(sqlite3.DatabaseError):
        verify_backup(backup)


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


def test_backup_refuses_unfinished_restore_with_partial_published_data(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    target = tmp_path / "target"
    target.mkdir()
    real_replace = os.replace

    def interrupt_spaces(source_path, target_path):
        if Path(target_path) == target / "spaces":
            raise OSError("interrupt after platform publish")
        return real_replace(source_path, target_path)

    monkeypatch.setattr(os, "replace", interrupt_spaces)
    with pytest.raises(OSError, match="interrupt"):
        restore_backup(backup, target)
    assert (target / ".platform").is_dir()
    assert not (target / "spaces").exists()
    assert restore_journal_path(target).is_file()

    partial_backup = tmp_path / "partial-backup"
    with pytest.raises(RuntimeError, match="unfinished restore"):
        create_backup(target, partial_backup)
    assert not partial_backup.exists()


def test_verify_rejects_unlisted_directory_symlink(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _platform, user, _article, _raw = seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = backup / "spaces" / user["workspace_root_name"] / "wiki" / "linked-directory"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symbolic links"):
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


def test_restore_hands_off_unpublished_trees_and_runtime_bottom_up(tmp_path: Path, monkeypatch):
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
        backup_restore, "_apply_fd_owner",
        lambda _descriptor, path, uid, gid: chowned.append((Path(path), uid, gid)),
    )

    restore_backup(backup, target, owner="wiki-service")

    install_paths = [path for path, _uid, _gid in chowned if "install-" in path.as_posix()]
    install_roots = [path for path in install_paths if path.parent.name == ".restore"]
    assert len(install_roots) == 2
    for root in install_roots:
        assert install_paths.index(root) > max(
            install_paths.index(path) for path in install_paths if path != root and root in path.parents
        )
    assert all((uid, gid) == (1234, 5678) for _path, uid, gid in chowned)
    assert all("/restore-" not in path.as_posix() for path, _uid, _gid in chowned)
    assert target / ".runtime" in {path for path, _uid, _gid in chowned}
    assert instance_lock_path(target) in {path for path, _uid, _gid in chowned}
    assert all(path.stat().st_mode & stat.S_IWUSR for root in (target / ".platform", target / "spaces") for path in (root, *root.rglob("*")))
    assert not (target / ".restore").exists()


def test_fd_owner_handoff_rejects_symlink_replacement(tmp_path: Path, monkeypatch):
    root = tmp_path / "install"
    root.mkdir()
    first = root / "a"
    later = root / "z"
    outside = tmp_path / "outside"
    first.write_text("first", encoding="utf-8")
    later.write_text("later", encoding="utf-8")
    outside.write_text("outside", encoding="utf-8")
    owner = backup_restore.pwd.getpwuid(os.getuid()).pw_name
    real_apply = backup_restore._apply_fd_owner

    def replace_later(descriptor: int, path: Path, uid: int, gid: int):
        real_apply(descriptor, path, uid, gid)
        if path == first:
            later.unlink()
            later.symlink_to(outside)

    monkeypatch.setattr(backup_restore, "_apply_fd_owner", replace_later)
    with pytest.raises(RuntimeError, match="symbolic links"):
        backup_restore._set_owner(root, backup_restore._owner_ids(owner))
    assert outside.read_text(encoding="utf-8") == "outside"


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


def test_restore_rejects_changed_numeric_identity_for_same_owner(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    target = tmp_path / "target"
    target.mkdir()
    current_uid = 1234

    monkeypatch.setattr(
        backup_restore.pwd, "getpwnam",
        lambda _owner: SimpleNamespace(pw_uid=current_uid, pw_gid=4321),
    )
    monkeypatch.setattr(
        backup_restore.grp, "getgrnam", lambda _owner: SimpleNamespace(gr_gid=5678),
    )
    monkeypatch.setattr(
        backup_restore, "_copy_restore_directory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected interruption")),
    )
    with pytest.raises(OSError, match="injected"):
        restore_backup(backup, target, owner="wiki-service")
    journal = json.loads(restore_journal_path(target).read_text(encoding="utf-8"))
    assert (journal["owner_uid"], journal["owner_gid"]) == (1234, 5678)

    current_uid = 2234
    with pytest.raises(RuntimeError, match="owner identity does not match"):
        restore_backup(backup, target, owner="wiki-service")
    unchanged = json.loads(restore_journal_path(target).read_text(encoding="utf-8"))
    assert (unchanged["owner_uid"], unchanged["owner_gid"]) == (1234, 5678)


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

    def fail_once(root: Path, selected_identity: tuple[int, int] | None):
        nonlocal failed
        if Path(root).name.startswith("install-.platform-") and not failed:
            failed = True
            raise PermissionError("injected chown failure")
        return real_set_owner(root, selected_identity)

    monkeypatch.setattr(backup_restore, "_set_owner", fail_once)
    with pytest.raises(PermissionError, match="injected chown"):
        restore_backup(backup, target, owner=owner)
    journal_path = restore_journal_path(target)
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["pending"] == [".platform", "spaces"]
    assert Path(journal["stage"]).is_dir()
    assert not (target / ".platform").exists()
    assert not (target / "spaces").exists()
    assert not list((target / ".restore").glob("install-*"))

    monkeypatch.setattr(backup_restore, "_set_owner", real_set_owner)
    restore_backup(backup, target, owner=owner)
    assert not journal_path.exists()
    assert (target / ".platform").stat().st_uid == os.getuid()
    assert (target / "spaces").stat().st_uid == os.getuid()


def test_runtime_owner_failure_keeps_journal_and_retry_restores_lock_access(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    target = tmp_path / "target"
    target.mkdir()
    owner = backup_restore.pwd.getpwuid(os.getuid()).pw_name
    real_runtime_owner = backup_restore._set_runtime_owner

    monkeypatch.setattr(
        backup_restore, "_set_runtime_owner",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("runtime handoff failed")),
    )
    with pytest.raises(PermissionError, match="runtime handoff"):
        restore_backup(backup, target, owner=owner)
    journal = json.loads(restore_journal_path(target).read_text(encoding="utf-8"))
    assert journal["pending"] == []
    assert (target / ".platform").is_dir()
    assert (target / "spaces").is_dir()

    monkeypatch.setattr(backup_restore, "_set_runtime_owner", real_runtime_owner)
    restore_backup(backup, target, owner=owner)
    assert not restore_journal_path(target).exists()
    assert (target / ".runtime").stat().st_uid == os.getuid()
    assert instance_lock_path(target).stat().st_uid == os.getuid()
    with InstanceLock(instance_lock_path(target)):
        pass


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


def test_restore_rejects_install_replacement_after_owner_handoff(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    target = tmp_path / "target"
    target.mkdir()
    owner = backup_restore.pwd.getpwuid(os.getuid()).pw_name
    real_set_owner = backup_restore._set_owner

    def replace_install(root: Path, selected_identity: tuple[int, int] | None):
        real_set_owner(root, selected_identity)
        if root.name.startswith("install-.platform-"):
            shutil_target = root.with_name(f"{root.name}-original")
            os.replace(root, shutil_target)
            root.mkdir()
            (root / "attacker.txt").write_text("replacement", encoding="utf-8")

    monkeypatch.setattr(backup_restore, "_set_owner", replace_install)
    with pytest.raises(RuntimeError, match="installation changed"):
        restore_backup(backup, target, owner=owner)
    assert restore_journal_path(target).is_file()
    assert not (target / ".platform").exists()
    assert not (target / "spaces").exists()


def test_completed_restore_residue_is_safe_to_backup_and_ignored_by_git(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    target = tmp_path / "target"
    target.mkdir()
    real_rmtree = backup_restore.shutil.rmtree

    def preserve_completed(path, *args, **kwargs):
        if Path(path).name.startswith(".restore-complete-"):
            raise OSError("cleanup interrupted")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(backup_restore.shutil, "rmtree", preserve_completed)
    with pytest.raises(OSError, match="cleanup interrupted"):
        restore_backup(backup, target)
    residues = list(target.glob(".restore-complete-*"))
    assert len(residues) == 1
    assert not restore_in_progress(target)
    assert (target / ".platform").is_dir()
    assert (target / "spaces").is_dir()

    completed_backup = tmp_path / "completed-backup"
    create_backup(target, completed_backup)
    verify_backup(completed_backup)

    repository = Path(__file__).resolve().parents[1]
    ignored = subprocess.run(
        [
            "git", "check-ignore", "--no-index",
            ".restore/master.key", ".restore-complete-deadbeef/master.key",
        ],
        cwd=repository, text=True, capture_output=True, check=True,
    ).stdout.splitlines()
    assert ignored == [".restore/master.key", ".restore-complete-deadbeef/master.key"]
