#!/usr/bin/env python3
"""Offline, checksummed backup and restore for a single Unlimited Wiki node."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
import grp
import pwd
import re
from datetime import datetime, timezone
from pathlib import Path


MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 1
DATA_DIRECTORIES = (".platform", "spaces")
RUNTIME_DIRECTORY = ".runtime"
RESTORE_WORK_DIRECTORY = ".restore"
INSTANCE_LOCK_NAME = "instance.lock"
RESTORE_JOURNAL_NAME = "restore.json"
RESTORE_JOURNAL_SCHEMA = 3


def instance_lock_path(project_root: Path) -> Path:
    return project_root.resolve() / RUNTIME_DIRECTORY / INSTANCE_LOCK_NAME


def restore_journal_path(project_root: Path) -> Path:
    return restore_work_path(project_root) / RESTORE_JOURNAL_NAME


def restore_work_path(project_root: Path) -> Path:
    return project_root.resolve() / RESTORE_WORK_DIRECTORY


def restore_in_progress(project_root: Path) -> bool:
    return restore_work_path(project_root).exists()


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        handle = self.path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("wiki service is running; stop it before backup or restore")
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.release()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_paths(root: Path) -> list[Path]:
    paths = [root / ".platform" / "platform.sqlite3"]
    spaces = root / "spaces"
    if spaces.is_dir() and not spaces.is_symlink():
        for workspace in spaces.iterdir():
            if not workspace.is_dir() or workspace.is_symlink():
                continue
            state_root = workspace / ".wiki-state"
            if not state_root.exists():
                continue
            if state_root.is_symlink() or not state_root.is_dir():
                raise RuntimeError("workspace state path must be a directory")
            # A missing or empty state directory is an uninitialized workspace.
            # Once any state artifact exists, the main database is mandatory.
            if any(state_root.iterdir()):
                paths.append(state_root / "state.sqlite3")
    return sorted(paths)


def _is_known_sqlite_path(root: Path, path: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    return (
        parts == (".platform", "platform.sqlite3")
        or (root.name == ".platform" and parts == ("platform.sqlite3",))
        or (
            root.name == "spaces"
            and len(parts) == 3
            and parts[1:] == (".wiki-state", "state.sqlite3")
        )
        or (
        len(parts) == 4
        and parts[0] == "spaces"
        and parts[2:] == (".wiki-state", "state.sqlite3")
        )
    )


def _is_sqlite_sidecar(root: Path, path: Path) -> bool:
    for suffix in ("-wal", "-shm"):
        if path.name.endswith(suffix):
            database = path.with_name(path.name[:-len(suffix)])
            return _is_known_sqlite_path(root, database)
    return False


def _check_sqlite(path: Path, *, checkpoint: bool) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"SQLite database is missing or invalid: {path.name}")
    with path.open("rb") as handle:
        if handle.read(16) != b"SQLite format 3\x00":
            raise sqlite3.DatabaseError(f"file is not a database: {path.name}")
    target = path if checkpoint else f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(target, uri=not checkpoint) as db:
        if checkpoint:
            busy, log_frames, checkpointed = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy or log_frames != checkpointed:
                raise RuntimeError(f"SQLite checkpoint is busy: {path.name}")
        result = db.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {path.name}")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError(f"SQLite foreign key check failed: {path.name}")
    if checkpoint:
        wal = Path(f"{path}-wal")
        if wal.exists() and wal.stat().st_size:
            raise RuntimeError(f"SQLite WAL was not fully checkpointed: {path.name}")


def _copy_data(source: Path, destination: Path) -> None:
    def ignore(directory, names):
        return {
            name for name in names
            if _is_sqlite_sidecar(source, Path(directory) / name)
        }

    for name in DATA_DIRECTORIES:
        source_path = source / name
        if source_path.exists():
            _validate_tree(source_path, "wiki data")
            shutil.copytree(source_path, destination / name, symlinks=True, ignore=ignore)


def _validate_tree(root: Path, label: str) -> None:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"{label} must contain regular files and directories only")
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise RuntimeError(f"{label} must not contain symbolic links")
        if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
            raise RuntimeError(f"{label} contains an unsupported file type")


def _manifest(root: Path, source_root: Path) -> dict:
    for name in DATA_DIRECTORIES:
        _validate_tree(root / name, "backup data")
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path != root / MANIFEST_NAME and not _is_sqlite_sidecar(root, path):
            relative = path.relative_to(root).as_posix()
            entries.append({"path": relative, "size": path.stat().st_size, "sha256": _sha256(path)})
    try:
        git_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=source_root,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        git_sha = None
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": git_sha,
        "files": entries,
    }


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json_atomic(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _owner_ids(owner: str | None) -> tuple[int, int] | None:
    if owner is None:
        return None
    try:
        account = pwd.getpwnam(owner)
    except KeyError as exc:
        raise RuntimeError(f"restore owner does not exist: {owner}") from exc
    try:
        group_id = grp.getgrnam(owner).gr_gid
    except KeyError:
        group_id = account.pw_gid
    return account.pw_uid, group_id


def _apply_fd_owner(descriptor: int, _path: Path, user_id: int, group_id: int) -> None:
    os.fchown(descriptor, user_id, group_id)


def _set_owner(root: Path, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    user_id, group_id = identity

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC

    def walk(descriptor: int, logical_path: Path) -> None:
        for name in sorted(os.listdir(descriptor)):
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            child_path = logical_path / name
            if stat.S_ISLNK(before.st_mode):
                raise RuntimeError("restore data must not contain symbolic links")
            if stat.S_ISDIR(before.st_mode):
                flags = directory_flags
            elif stat.S_ISREG(before.st_mode):
                flags = file_flags
            else:
                raise RuntimeError("restore data contains an unsupported file type")
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise RuntimeError("restore data changed during ownership handoff")
                if stat.S_ISDIR(opened.st_mode):
                    walk(child, child_path)
                _apply_fd_owner(child, child_path, user_id, group_id)
            finally:
                os.close(child)

    root_descriptor = os.open(root, directory_flags)
    try:
        walk(root_descriptor, root)
        _apply_fd_owner(root_descriptor, root, user_id, group_id)
    finally:
        os.close(root_descriptor)


def _set_runtime_owner(
    runtime: Path, lock_path: Path, identity: tuple[int, int] | None,
) -> None:
    if identity is None:
        return
    user_id, group_id = identity
    file_descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _apply_fd_owner(file_descriptor, lock_path, user_id, group_id)
    finally:
        os.close(file_descriptor)
    runtime_descriptor = os.open(
        runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        _apply_fd_owner(runtime_descriptor, runtime, user_id, group_id)
    finally:
        os.close(runtime_descriptor)


def _ensure_private_restore_root(
    project_root: Path, identity: tuple[int, int] | None,
) -> Path:
    work_root = restore_work_path(project_root)
    if os.geteuid() == 0 and identity is not None and identity[0] != 0:
        project_mode = project_root.stat().st_mode
        if project_root.stat().st_uid == identity[0] or project_mode & 0o022:
            raise RuntimeError("restore project root must not be writable by the service account")
    if work_root.is_symlink():
        raise RuntimeError("restore work directory must not be a symbolic link")
    work_root.mkdir(mode=0o700, exist_ok=True)
    info = work_root.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("restore work directory must be private to the restore process")
    return work_root


def _load_restore_journal(path: Path, work_root: Path, backup_root: Path) -> tuple[dict, Path]:
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("restore journal is invalid") from exc
    expected_keys = {
        "schema_version", "backup_manifest_sha256", "stage_manifest_sha256", "stage", "pending",
        "owner", "owner_uid", "owner_gid",
    }
    pending = journal.get("pending")
    if (
        set(journal) != expected_keys
        or journal.get("schema_version") != RESTORE_JOURNAL_SCHEMA
        or not isinstance(journal.get("backup_manifest_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", journal["backup_manifest_sha256"])
        or not isinstance(journal.get("stage_manifest_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", journal["stage_manifest_sha256"])
        or not isinstance(journal.get("stage"), str)
        or (journal.get("owner") is not None and not isinstance(journal.get("owner"), str))
        or (journal.get("owner_uid") is not None and type(journal.get("owner_uid")) is not int)
        or (journal.get("owner_gid") is not None and type(journal.get("owner_gid")) is not int)
        or (journal.get("owner") is None) != (journal.get("owner_uid") is None)
        or (journal.get("owner") is None) != (journal.get("owner_gid") is None)
        or (journal.get("owner_uid") is not None and journal["owner_uid"] < 0)
        or (journal.get("owner_gid") is not None and journal["owner_gid"] < 0)
        or not isinstance(pending, list)
        or any(not isinstance(item, str) for item in pending)
        or len(pending) != len(set(pending))
        or not set(pending).issubset(DATA_DIRECTORIES)
    ):
        raise RuntimeError("restore journal is invalid")
    stage_literal = Path(journal["stage"])
    if (
        not stage_literal.is_absolute()
        or not re.fullmatch(r"restore-[0-9a-f]{32}", stage_literal.name)
        or stage_literal.is_symlink()
        or not stage_literal.is_dir()
    ):
        raise RuntimeError("restore journal stage is invalid")
    try:
        stage = stage_literal.resolve(strict=True)
        work_root_resolved = work_root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("restore journal stage is invalid") from exc
    if stage.parent != work_root_resolved:
        raise RuntimeError("restore journal stage is outside the private restore directory")
    if journal["backup_manifest_sha256"] != _sha256(backup_root / MANIFEST_NAME):
        raise RuntimeError("an unfinished restore exists for a different backup")
    _verify_restore_stage(stage, journal["stage_manifest_sha256"])
    return journal, stage


def _verify_restore_stage(stage: Path, expected_manifest_sha256: str) -> None:
    stage_manifest = stage / MANIFEST_NAME
    if (
        stage.is_symlink()
        or not stage.is_dir()
        or stage_manifest.is_symlink()
        or expected_manifest_sha256 != _sha256(stage_manifest)
    ):
        raise RuntimeError("restore journal stage manifest is invalid")
    verify_backup(stage)


def _copy_restore_directory(
    source: Path, target: Path, work_root: Path, *,
    stage: Path, stage_manifest_sha256: str, identity: tuple[int, int] | None,
) -> None:
    temporary = work_root / f"install-{target.name}-{uuid.uuid4().hex}"

    def ignore(directory, names):
        return {
            name for name in names
            if _is_sqlite_sidecar(source, Path(directory) / name)
        }

    try:
        shutil.copytree(source, temporary, symlinks=True, ignore=ignore)
        if not _directory_matches(source, temporary):
            raise RuntimeError("restore staging changed during installation")
        _fsync_tree(temporary)
        _verify_restore_stage(stage, stage_manifest_sha256)
        _set_owner(temporary, identity)
        if not _directory_matches(source, temporary):
            raise RuntimeError("restore installation changed during ownership handoff")
        _verify_restore_stage(stage, stage_manifest_sha256)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _directory_matches(source: Path, target: Path) -> bool:
    if target.is_symlink() or not target.is_dir():
        return False
    if any(path.is_symlink() for path in target.rglob("*")):
        return False
    source_files = {
        path.relative_to(source).as_posix(): path
        for path in source.rglob("*")
        if path.is_file() and not path.is_symlink() and not _is_sqlite_sidecar(source, path)
    }
    target_files = {
        path.relative_to(target).as_posix(): path
        for path in target.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and not _is_sqlite_sidecar(source, source / path.relative_to(target))
    }
    if set(source_files) != set(target_files):
        return False
    return all(
        source_files[name].stat().st_size == target_files[name].stat().st_size
        and _sha256(source_files[name]) == _sha256(target_files[name])
        for name in source_files
    )


def _prepare_restore_stage(backup_root: Path, project_root: Path, stage: Path) -> dict:
    stage.mkdir(mode=0o700)
    shutil.copy2(backup_root / MANIFEST_NAME, stage / MANIFEST_NAME)
    _copy_data(backup_root, stage)
    # Verify the private copy before applying the documented session revocation.
    verify_backup(stage)
    platform_db = stage / ".platform" / "platform.sqlite3"
    with sqlite3.connect(platform_db) as db:
        db.execute("DELETE FROM sessions")
    for path in _sqlite_paths(stage):
        _check_sqlite(path, checkpoint=True)
    manifest = _manifest(stage, project_root)
    _write_json_atomic(stage / MANIFEST_NAME, manifest)
    verify_backup(stage)
    _fsync_tree(stage)
    return manifest


def verify_backup(backup_root: Path) -> dict:
    if backup_root.is_symlink():
        raise RuntimeError("backup root must not be a symbolic link")
    backup_root = backup_root.resolve()
    allowed_root_entries = {*DATA_DIRECTORIES, MANIFEST_NAME}
    if any(path.name not in allowed_root_entries for path in backup_root.iterdir()):
        raise RuntimeError("backup root contains unsupported entries")
    for name in DATA_DIRECTORIES:
        _validate_tree(backup_root / name, "backup data")
    manifest_path = backup_root / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("backup manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION or not isinstance(manifest.get("files"), list):
        raise RuntimeError("backup manifest is unsupported")
    for item in manifest["files"]:
        raw_path = item.get("path") if isinstance(item, dict) else None
        relative = Path(raw_path) if isinstance(raw_path, str) else None
        if (
            not isinstance(item, dict)
            or relative is None
            or relative.is_absolute()
            or ".." in relative.parts
            or len(relative.parts) < 2
            or relative.parts[0] not in DATA_DIRECTORIES
            or not isinstance(item.get("size"), int)
            or item["size"] < 0
            or not isinstance(item.get("sha256"), str)
            or len(item["sha256"]) != 64
        ):
            raise RuntimeError("backup manifest contains an invalid file entry")
    expected = {item["path"]: item for item in manifest["files"]}
    if len(expected) != len(manifest["files"]):
        raise RuntimeError("backup manifest contains duplicate file entries")
    actual = {
        path.relative_to(backup_root).as_posix(): path
        for path in backup_root.rglob("*")
        if path.is_file()
        and path != manifest_path
        and not _is_sqlite_sidecar(backup_root, path)
    }
    if set(actual) != set(expected):
        raise RuntimeError("backup file set does not match manifest")
    for relative, path in actual.items():
        item = expected[relative]
        if path.is_symlink() or path.stat().st_size != item["size"] or _sha256(path) != item["sha256"]:
            raise RuntimeError(f"backup checksum mismatch: {relative}")
    key = backup_root / ".platform" / "master.key"
    if not key.is_file() or key.stat().st_size != 32:
        raise RuntimeError("backup platform master key is missing or invalid")
    for path in _sqlite_paths(backup_root):
        _check_sqlite(path, checkpoint=False)
    return manifest


def create_backup(project_root: Path, destination: Path) -> dict:
    project_root = project_root.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise RuntimeError("backup destination already exists")
    for name in DATA_DIRECTORIES:
        source_data = (project_root / name).resolve()
        if _contains(source_data, destination):
            raise RuntimeError("backup destination must be outside wiki data directories")
    lock = InstanceLock(instance_lock_path(project_root))
    stage = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4().hex}")
    with lock:
        if restore_in_progress(project_root):
            raise RuntimeError("an unfinished restore exists; finish it before creating a backup")
        key = project_root / ".platform" / "master.key"
        if not key.is_file() or key.stat().st_size != 32:
            raise RuntimeError("platform master key is missing or invalid")
        for name in DATA_DIRECTORIES:
            _validate_tree(project_root / name, "wiki data")
        for path in _sqlite_paths(project_root):
            _check_sqlite(path, checkpoint=True)
        try:
            stage.mkdir(mode=0o700, parents=True)
            _copy_data(project_root, stage)
            manifest = _manifest(stage, project_root)
            (stage / MANIFEST_NAME).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
            os.chmod(stage / MANIFEST_NAME, 0o600)
            verify_backup(stage)
            _fsync_tree(stage)
            os.replace(stage, destination)
            _fsync_directory(destination.parent)
            return manifest
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise


def restore_backup(backup_root: Path, project_root: Path, *, owner: str | None = None) -> dict:
    backup_root = backup_root.resolve()
    project_root = project_root.resolve()
    identity = _owner_ids(owner)
    project_root.mkdir(parents=True, exist_ok=True)
    runtime = project_root / RUNTIME_DIRECTORY
    if runtime.is_symlink():
        raise RuntimeError("instance runtime directory must not be a symbolic link")
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = InstanceLock(instance_lock_path(project_root))
    journal_path = restore_journal_path(project_root)
    with lock:
        if not journal_path.exists() and any((project_root / name).exists() for name in DATA_DIRECTORIES):
            raise RuntimeError("restore target already contains wiki platform data")
        work_root = _ensure_private_restore_root(project_root, identity)
        if not journal_path.exists() and any(work_root.iterdir()):
            raise RuntimeError("restore work directory contains no valid journal")
        if journal_path.exists():
            if journal_path.is_symlink():
                raise RuntimeError("restore journal is invalid")
            journal, old_stage = _load_restore_journal(journal_path, work_root, backup_root)
            if journal["owner"] != owner:
                raise RuntimeError("restore owner does not match the unfinished restore")
            journal_identity = None if journal["owner_uid"] is None else (
                journal["owner_uid"], journal["owner_gid"],
            )
            if journal_identity != identity:
                raise RuntimeError("restore owner identity does not match the unfinished restore")
            pending = set(journal["pending"])
            for name in DATA_DIRECTORIES:
                if name in pending:
                    continue
                source = old_stage / name
                target = project_root / name
                if (
                    not source.is_dir()
                    or source.is_symlink()
                    or not _directory_matches(source, target)
                ):
                    raise RuntimeError(f"restore state is inconsistent for {name}")
            stage = work_root / f"restore-{uuid.uuid4().hex}"
            try:
                manifest = _prepare_restore_stage(backup_root, project_root, stage)
                journal["stage"] = str(stage)
                journal["stage_manifest_sha256"] = _sha256(stage / MANIFEST_NAME)
                _write_json_atomic(journal_path, journal)
                shutil.rmtree(old_stage)
                _fsync_directory(work_root)
            except BaseException:
                shutil.rmtree(stage, ignore_errors=True)
                raise
        else:
            stage = work_root / f"restore-{uuid.uuid4().hex}"
            try:
                manifest = _prepare_restore_stage(backup_root, project_root, stage)
                journal = {
                    "schema_version": RESTORE_JOURNAL_SCHEMA,
                    "backup_manifest_sha256": _sha256(backup_root / MANIFEST_NAME),
                    "stage_manifest_sha256": _sha256(stage / MANIFEST_NAME),
                    "stage": str(stage),
                    "pending": list(DATA_DIRECTORIES),
                    "owner": owner,
                    "owner_uid": identity[0] if identity is not None else None,
                    "owner_gid": identity[1] if identity is not None else None,
                }
                _write_json_atomic(journal_path, journal)
            except BaseException:
                shutil.rmtree(stage, ignore_errors=True)
                if not any(work_root.iterdir()):
                    work_root.rmdir()
                raise

        for name in list(journal["pending"]):
            source = stage / name
            target = project_root / name
            if not source.is_dir() or source.is_symlink():
                raise RuntimeError(f"restore state is inconsistent for {name}")
            if not target.exists():
                _copy_restore_directory(
                    source, target, work_root,
                    stage=stage,
                    stage_manifest_sha256=journal["stage_manifest_sha256"],
                    identity=identity,
                )
            elif not _directory_matches(source, target):
                raise RuntimeError(f"restore state is inconsistent for {name}")
            journal["pending"].remove(name)
            _write_json_atomic(journal_path, journal)

        _set_runtime_owner(runtime, instance_lock_path(project_root), identity)
        completed = project_root / f".restore-complete-{uuid.uuid4().hex}"
        os.replace(work_root, completed)
        _fsync_directory(project_root)
        shutil.rmtree(completed)
        _fsync_directory(project_root)
        return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--project-root", type=Path, default=Path.cwd())
    backup_parser.add_argument("--output", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("backup", type=Path)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("backup", type=Path)
    restore_parser.add_argument("--project-root", type=Path, required=True)
    restore_parser.add_argument("--owner", help="system account that will own restored data")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            result = create_backup(args.project_root, args.output)
        elif args.command == "verify":
            result = verify_backup(args.backup)
        else:
            result = restore_backup(args.backup, args.project_root, owner=args.owner)
    except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "schema_version": result["schema_version"], "files": len(result["files"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
