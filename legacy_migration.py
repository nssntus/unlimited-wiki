"""Reversible migration of the legacy single workspace into the first tenant."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path, PurePosixPath

from model_settings import load_model_settings
from platform_store import PlatformStore


SKIP_STATE_NAMES = {"write.lock", "state.sqlite3-wal", "state.sqlite3-shm", "model-settings.json"}
MIGRATION_KIND = "legacy-single-workspace"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _legacy_files(project_root: Path) -> list[tuple[Path, str]]:
    rows: list[tuple[Path, str]] = []
    for directory in ("wiki", "raw", ".wiki-state"):
        root = project_root / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if directory == ".wiki-state" and path.name in SKIP_STATE_NAMES:
                continue
            rows.append((path, path.relative_to(project_root).as_posix()))
    return rows


def _copy_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db, sqlite3.connect(destination) as target_db:
        source_db.backup(target_db)
        if target_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("legacy state database failed integrity verification")


def _sqlite_counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}


def _is_symlink_free_under(base: Path, candidate: Path) -> bool:
    base = Path(os.path.abspath(base))
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(base)
    except ValueError:
        return False
    current = base
    if current.is_symlink():
        return False
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def _manifest_files_match(root: Path, manifest: dict, hash_key: str) -> bool:
    if root.is_symlink() or not root.is_dir() or not isinstance(manifest.get("files"), list) or not manifest["files"]:
        return False
    expected: dict[str, str] = {}
    for item in manifest["files"]:
        if not isinstance(item, dict):
            return False
        relative = item.get("path")
        expected_hash = item.get(hash_key)
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            return False
        normalized = PurePosixPath(relative)
        if normalized.is_absolute() or ".." in normalized.parts or normalized.as_posix() != relative:
            return False
        if relative in expected:
            return False
        expected[relative] = expected_hash

    actual: dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            return False
        if path.is_file():
            actual[path.relative_to(root).as_posix()] = path
    if set(actual) != set(expected):
        return False
    return all(digest(actual[relative]) == expected_hash for relative, expected_hash in expected.items())


def _published_manifest(platform: PlatformStore, workspace: dict, existing: dict | None) -> dict | None:
    if existing is None:
        return None
    try:
        manifest = json.loads(existing["manifest_json"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(manifest, dict)
        or manifest.get("id") != existing["id"]
        or manifest.get("workspace_id") != workspace["id"]
        or manifest.get("status") != existing["status"]
    ):
        return None
    backup_value = manifest.get("backup")
    if not isinstance(backup_value, str):
        return None
    backup_relative = PurePosixPath(backup_value)
    if (
        backup_relative.is_absolute()
        or ".." in backup_relative.parts
        or backup_relative.as_posix() != backup_value
    ):
        return None
    expected_backup = platform.state_root / "migrations" / existing["id"] / "backup"
    backup = platform.project_root.joinpath(*backup_relative.parts)
    if Path(os.path.abspath(backup)) != Path(os.path.abspath(expected_backup)):
        return None
    if not _is_symlink_free_under(platform.state_root, backup):
        return None
    target = platform.spaces_root / workspace["root_name"]
    if not _is_symlink_free_under(platform.spaces_root, target):
        return None
    if not _manifest_files_match(target, manifest, "target_sha256"):
        return None
    if not _manifest_files_match(backup, manifest, "backup_sha256"):
        return None
    return manifest


def _finalize_published_migration(
    platform: PlatformStore, user_id: str, workspace: dict, manifest: dict,
) -> dict:
    committed = {**manifest, "status": "committed"}
    platform.finalize_migration(
        committed["id"], MIGRATION_KIND, workspace["id"], committed,
        actor_id=user_id, file_count=len(committed["files"]),
    )
    return committed


def migrate_legacy_workspace(platform: PlatformStore, user_id: str, *, fail_after: int | None = None) -> dict:
    existing = platform.migration(MIGRATION_KIND)
    if existing is None and not _legacy_files(platform.project_root):
        return {"status": "not_needed", "files": []}

    lock_path = platform.project_root / ".runtime" / "legacy-migration.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(lock_path.parent, 0o700)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            return _migrate_legacy_workspace_locked(platform, user_id, fail_after=fail_after)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _migrate_legacy_workspace_locked(platform: PlatformStore, user_id: str, *, fail_after: int | None = None) -> dict:
    existing = platform.migration(MIGRATION_KIND)
    workspace = platform.user_workspace(user_id)
    target = platform.spaces_root / workspace["root_name"]
    if not _is_symlink_free_under(platform.spaces_root, target):
        raise RuntimeError("legacy migration target path is not canonical; repair is required")
    published = _published_manifest(platform, workspace, existing)
    if existing is not None and existing["status"] == "committed":
        if published is None:
            raise RuntimeError("committed legacy migration does not match its manifest; repair is required")
        return published
    if published is not None:
        return _finalize_published_migration(platform, user_id, workspace, published)
    if existing is not None and target.exists() and any(
        path.is_file() or path.is_symlink() for path in target.rglob("*")
    ):
        raise RuntimeError("published legacy migration does not match its manifest; repair is required")
    files = _legacy_files(platform.project_root)
    if not files:
        return {"status": "not_needed", "files": []}

    migration_id = existing["id"] if existing is not None else uuid.uuid4().hex
    staging = target.with_name(target.name + ".migrating")
    backup = platform.state_root / "migrations" / migration_id / "backup"
    if staging.exists():
        shutil.rmtree(staging)
    if backup.exists():
        shutil.rmtree(backup)
    staging.mkdir(parents=True)
    backup.mkdir(parents=True)
    manifest = {"id": migration_id, "status": "copying", "workspace_id": workspace["id"], "files": []}
    platform.record_migration(migration_id, MIGRATION_KIND, workspace["id"], "copying", manifest)
    copied = 0
    target_published = False
    try:
        for source, relative in files:
            staged = staging / relative
            backed_up = backup / relative
            if relative == ".wiki-state/state.sqlite3":
                _copy_sqlite(source, staged)
                _copy_sqlite(source, backed_up)
                if _sqlite_counts(source) != _sqlite_counts(staged) or _sqlite_counts(source) != _sqlite_counts(backed_up):
                    raise RuntimeError("legacy state database row counts do not match")
            else:
                staged.parent.mkdir(parents=True, exist_ok=True)
                backed_up.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, staged)
                shutil.copy2(source, backed_up)
            expected = digest(source)
            if relative != ".wiki-state/state.sqlite3" and (digest(staged) != expected or digest(backed_up) != expected):
                raise RuntimeError(f"migration hash mismatch: {relative}")
            manifest["files"].append({
                "path": relative, "size": source.stat().st_size, "source_sha256": expected,
                "target_sha256": digest(staged), "backup_sha256": digest(backed_up),
            })
            copied += 1
            if fail_after is not None and copied >= fail_after:
                raise RuntimeError("injected migration failure")

        legacy_model = load_model_settings(platform.project_root)
        if legacy_model.configured:
            platform.save_model(workspace["id"], legacy_model.provider, legacy_model.base_url, legacy_model.api_key, legacy_model.model)

        manifest["status"] = "prepared"
        manifest["backup"] = backup.relative_to(platform.project_root).as_posix()
        platform.record_migration(migration_id, MIGRATION_KIND, workspace["id"], "prepared", manifest)

        # The target was created with empty wiki/raw directories at registration.
        for child in (target / "wiki", target / "raw"):
            if child.exists():
                child.rmdir()
        if target.exists():
            target.rmdir()
        os.replace(staging, target)
        target_published = True
        return _finalize_published_migration(platform, user_id, workspace, manifest)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        if not target_published:
            manifest["status"] = "rolled_back"
            platform.record_migration(migration_id, MIGRATION_KIND, workspace["id"], "rolled_back", manifest)
        raise
