"""Crash-recoverable file transactions for the Markdown knowledge base."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator


_PROCESS_LOCK = threading.RLock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def safe_project_rel(rel: str) -> str:
    if not isinstance(rel, str):
        raise ValueError("path must be a string")
    text = rel.strip()
    if not text or "\\" in text or any(ord(ch) < 32 for ch in text):
        raise ValueError("invalid path")
    pure = PurePosixPath(text)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("invalid path")
    if pure.as_posix() != text:
        raise ValueError("path must be canonical")
    return text


class FileStore:
    """Serializes Markdown writes and keeps exact before-images for recovery."""

    def __init__(self, project_root: Path):
        self.root = project_root.resolve()
        self.state_root = self.root / ".wiki-state"
        self.history_root = self.state_root / "history"
        self.history_root.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.state_root / "write.lock"
        self.lock_path.touch(exist_ok=True)
        self.recover()

    def resolve(self, rel: str) -> Path:
        clean = safe_project_rel(rel)
        target = (self.root / clean).resolve(strict=False)
        if not target.is_relative_to(self.root):
            raise ValueError("path escapes project root")
        return target

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        with _PROCESS_LOCK:
            with self.lock_path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _write_manifest(self, directory: Path, manifest: dict) -> None:
        atomic_write(
            directory / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
        )

    def commit(
        self,
        changes: dict[str, str | bytes | None],
        *,
        kind: str,
        metadata: dict | None = None,
        operation_id: str | None = None,
        must_not_exist: bool = False,
    ) -> dict:
        normalized: dict[str, bytes | None] = {}
        for rel, value in changes.items():
            clean = safe_project_rel(rel)
            if clean.startswith(".wiki-state/"):
                raise ValueError("business writes cannot target runtime state")
            normalized[clean] = value.encode("utf-8") if isinstance(value, str) else value
        if not normalized:
            raise ValueError("empty transaction")

        op_id = operation_id or uuid.uuid4().hex
        op_dir = self.history_root / op_id
        with self.locked():
            if must_not_exist and any(self.resolve(rel).exists() for rel in normalized):
                raise FileExistsError("transaction target already exists")
            if op_dir.exists():
                raise FileExistsError(f"operation already exists: {op_id}")
            backups = op_dir / "before"
            backups.mkdir(parents=True)
            entries: list[dict] = []
            for index, (rel, after) in enumerate(sorted(normalized.items())):
                path = self.resolve(rel)
                existed = path.is_file()
                before = path.read_bytes() if existed else None
                if before is not None:
                    atomic_write(backups / f"{index:04d}.bin", before)
                entries.append(
                    {
                        "path": rel,
                        "backup": f"before/{index:04d}.bin" if existed else None,
                        "before_sha256": sha256(before) if before is not None else None,
                        "after_sha256": sha256(after) if after is not None else None,
                        "delete": after is None,
                    }
                )
            manifest = {
                "operation_id": op_id,
                "kind": kind,
                "created_at": utc_now(),
                "status": "prepared",
                "metadata": metadata or {},
                "entries": entries,
            }
            self._write_manifest(op_dir, manifest)
            manifest["status"] = "applying"
            self._write_manifest(op_dir, manifest)
            try:
                for rel, after in sorted(normalized.items()):
                    path = self.resolve(rel)
                    if after is None:
                        with contextlib.suppress(FileNotFoundError):
                            path.unlink()
                            _fsync_dir(path.parent)
                    else:
                        atomic_write(path, after)
                manifest["status"] = "committed"
                manifest["committed_at"] = utc_now()
                self._write_manifest(op_dir, manifest)
            except BaseException:
                self._restore_manifest(op_dir, manifest)
                manifest["status"] = "rolled_back"
                manifest["rolled_back_at"] = utc_now()
                self._write_manifest(op_dir, manifest)
                raise
        return manifest

    def _restore_manifest(self, op_dir: Path, manifest: dict) -> None:
        for entry in reversed(manifest.get("entries", [])):
            path = self.resolve(entry["path"])
            backup = entry.get("backup")
            if backup:
                atomic_write(path, (op_dir / backup).read_bytes())
            else:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                    _fsync_dir(path.parent)

    def recover(self) -> list[str]:
        recovered: list[str] = []
        with self.locked():
            for manifest_path in sorted(self.history_root.glob("*/manifest.json")):
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if manifest.get("status") not in {"prepared", "applying"}:
                    continue
                self._restore_manifest(manifest_path.parent, manifest)
                manifest["status"] = "recovered"
                manifest["recovered_at"] = utc_now()
                self._write_manifest(manifest_path.parent, manifest)
                recovered.append(manifest["operation_id"])
        return recovered

    def operation(self, operation_id: str) -> dict:
        if not operation_id or not re.fullmatch(r"[A-Za-z0-9-]+", operation_id):
            raise ValueError("invalid operation id")
        path = self.history_root / operation_id / "manifest.json"
        if not path.is_file():
            raise FileNotFoundError(operation_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def rollback(self, operation_id: str) -> dict:
        with self.locked():
            manifest = self.operation(operation_id)
            if manifest.get("status") != "committed":
                raise ValueError("operation is not rollbackable")
            for entry in manifest.get("entries", []):
                path = self.resolve(entry["path"])
                current = sha256(path.read_bytes()) if path.is_file() else None
                if current != entry.get("after_sha256"):
                    raise RuntimeError(f"file changed after operation: {entry['path']}")
            self._restore_manifest(self.history_root / operation_id, manifest)
            manifest["status"] = "rolled_back"
            manifest["rolled_back_at"] = utc_now()
            self._write_manifest(self.history_root / operation_id, manifest)
            return manifest
