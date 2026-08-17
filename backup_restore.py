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
import subprocess
import sys
import uuid
import grp
import pwd
from datetime import datetime, timezone
from pathlib import Path


MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 1
DATA_DIRECTORIES = (".platform", "spaces")
RUNTIME_DIRECTORY = ".runtime"
INSTANCE_LOCK_NAME = "instance.lock"
RESTORE_JOURNAL_NAME = "restore.json"


def instance_lock_path(project_root: Path) -> Path:
    return project_root.resolve() / RUNTIME_DIRECTORY / INSTANCE_LOCK_NAME


def restore_journal_path(project_root: Path) -> Path:
    return project_root.resolve() / RUNTIME_DIRECTORY / RESTORE_JOURNAL_NAME


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
    return sorted(path for path in root.rglob("*.sqlite3") if path.is_file() and not path.is_symlink())


def _check_sqlite(path: Path, *, checkpoint: bool) -> None:
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
    def ignore(_directory, names):
        return {
            name for name in names
            if name.endswith("-wal") or name.endswith("-shm")
        }

    for name in DATA_DIRECTORIES:
        source_path = source / name
        if source_path.exists():
            if source_path.is_symlink() or any(path.is_symlink() for path in source_path.rglob("*")):
                raise RuntimeError("wiki data must not contain symbolic links")
            shutil.copytree(source_path, destination / name, symlinks=True, ignore=ignore)


def _manifest(root: Path, source_root: Path) -> dict:
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("backup data must not contain symbolic links")
        if path.is_file() and path.name != MANIFEST_NAME:
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


def _set_owner(root: Path, owner: str | None) -> None:
    if owner is None:
        return
    try:
        account = pwd.getpwnam(owner)
    except KeyError as exc:
        raise RuntimeError(f"restore owner does not exist: {owner}") from exc
    try:
        group_id = grp.getgrnam(owner).gr_gid
    except KeyError:
        group_id = account.pw_gid
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            raise RuntimeError("restore data must not contain symbolic links")
        os.chown(path, account.pw_uid, group_id)


def verify_backup(backup_root: Path) -> dict:
    if backup_root.is_symlink():
        raise RuntimeError("backup root must not be a symbolic link")
    backup_root = backup_root.resolve()
    manifest_path = backup_root / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("backup manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION or not isinstance(manifest.get("files"), list):
        raise RuntimeError("backup manifest is unsupported")
    for item in manifest["files"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or Path(item["path"]).is_absolute()
            or ".." in Path(item["path"]).parts
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
        if path.is_file() and path.name != MANIFEST_NAME
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
    key = project_root / ".platform" / "master.key"
    if not key.is_file() or key.stat().st_size != 32:
        raise RuntimeError("platform master key is missing or invalid")
    lock = InstanceLock(instance_lock_path(project_root))
    stage = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4().hex}")
    with lock:
        for path in _sqlite_paths(project_root / ".platform") + _sqlite_paths(project_root / "spaces"):
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
    project_root.mkdir(parents=True, exist_ok=True)
    runtime = project_root / RUNTIME_DIRECTORY
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = InstanceLock(instance_lock_path(project_root))
    journal_path = restore_journal_path(project_root)
    with lock:
        if journal_path.exists():
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if journal.get("backup_manifest_sha256") != _sha256(backup_root / MANIFEST_NAME):
                raise RuntimeError("an unfinished restore exists for a different backup")
            stage = Path(journal["stage"])
            manifest = json.loads((stage / MANIFEST_NAME).read_text(encoding="utf-8"))
        else:
            if any((project_root / name).exists() for name in DATA_DIRECTORIES):
                raise RuntimeError("restore target already contains wiki platform data")
            stage = runtime / f"restore-{uuid.uuid4().hex}"
            try:
                stage.mkdir(mode=0o700)
                shutil.copy2(backup_root / MANIFEST_NAME, stage / MANIFEST_NAME)
                _copy_data(backup_root, stage)
                # Verify the private copy, closing the source verification/copy race.
                manifest = verify_backup(stage)
                platform_db = stage / ".platform" / "platform.sqlite3"
                with sqlite3.connect(platform_db) as db:
                    db.execute("DELETE FROM sessions")
                for path in _sqlite_paths(stage):
                    _check_sqlite(path, checkpoint=True)
                _set_owner(stage, owner)
                _fsync_tree(stage)
                journal = {
                    "schema_version": 1,
                    "backup_manifest_sha256": _sha256(backup_root / MANIFEST_NAME),
                    "stage": str(stage),
                    "pending": list(DATA_DIRECTORIES),
                }
                _write_json_atomic(journal_path, journal)
            except BaseException:
                shutil.rmtree(stage, ignore_errors=True)
                raise

        for name in list(journal["pending"]):
            source = stage / name
            target = project_root / name
            if source.exists() and not target.exists():
                os.replace(source, target)
                _fsync_directory(project_root)
            elif source.exists() or not target.exists():
                raise RuntimeError(f"restore state is inconsistent for {name}")
            journal["pending"].remove(name)
            _write_json_atomic(journal_path, journal)

        journal_path.unlink()
        _fsync_directory(runtime)
        shutil.rmtree(stage, ignore_errors=True)
        _set_owner(runtime, owner)
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
