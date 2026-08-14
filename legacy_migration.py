"""Reversible migration of the legacy single workspace into the first tenant."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

from model_settings import load_model_settings
from platform_store import PlatformStore


SKIP_STATE_NAMES = {"write.lock", "state.sqlite3-wal", "state.sqlite3-shm", "model-settings.json"}


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


def migrate_legacy_workspace(platform: PlatformStore, user_id: str, *, fail_after: int | None = None) -> dict:
    lock_path = platform.project_root / ".wiki-state" / "write.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            return _migrate_legacy_workspace_locked(platform, user_id, fail_after=fail_after)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _migrate_legacy_workspace_locked(platform: PlatformStore, user_id: str, *, fail_after: int | None = None) -> dict:
    existing = platform.migration("legacy-single-workspace")
    if existing and existing["status"] == "committed":
        return json.loads(existing["manifest_json"])
    files = _legacy_files(platform.project_root)
    if not files:
        return {"status": "not_needed", "files": []}

    workspace = platform.user_workspace(user_id)
    target = platform.workspace_root(workspace["root_name"])
    migration_id = uuid.uuid4().hex
    staging = target.with_name(target.name + ".migrating")
    backup = platform.state_root / "migrations" / migration_id / "backup"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    backup.mkdir(parents=True)
    manifest = {"id": migration_id, "status": "copying", "workspace_id": workspace["id"], "files": []}
    platform.record_migration(migration_id, "legacy-single-workspace", workspace["id"], "copying", manifest)
    copied = 0
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

        # The target was created with empty wiki/raw directories at registration.
        for child in (target / "wiki", target / "raw"):
            if child.exists():
                child.rmdir()
        if target.exists():
            target.rmdir()
        os.replace(staging, target)
        manifest["status"] = "committed"
        manifest["backup"] = backup.relative_to(platform.project_root).as_posix()
        platform.record_migration(migration_id, "legacy-single-workspace", workspace["id"], "committed", manifest)
        platform.audit(user_id, "workspace.migrate_legacy", "workspace", workspace["id"], {"migration_id": migration_id, "files": len(files)})
        return manifest
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        manifest["status"] = "rolled_back"
        platform.record_migration(migration_id, "legacy-single-workspace", workspace["id"], "rolled_back", manifest)
        raise
