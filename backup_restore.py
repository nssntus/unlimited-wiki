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

from square_v2 import normalize_taxonomy_name, taxonomy_normalized_key


MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 2
DATA_DIRECTORIES = (".platform", "spaces")
RUNTIME_DIRECTORY = ".runtime"
RESTORE_WORK_DIRECTORY = ".restore"
INSTANCE_LOCK_NAME = "instance.lock"
RESTORE_JOURNAL_NAME = "restore.json"
RESTORE_JOURNAL_SCHEMA = 3

PLATFORM_SCHEMA_ANCHORS = {
    "users": {"id", "email", "nickname", "password_hash", "role", "status", "created_at"},
    "workspaces": {"id", "owner_id", "root_name", "display_name", "created_at"},
    "sessions": {"token_hash", "user_id", "csrf_hash", "expires_at", "created_at"},
    "model_settings": {
        "workspace_id", "provider", "base_url_enc", "api_key_enc", "model", "updated_at",
    },
    "recovery_codes": {"code_hash", "user_id", "expires_at", "used_at"},
    "login_attempts": {"scope_hash", "failures", "blocked_until", "updated_at"},
    "share_previews": {
        "id", "owner_id", "workspace_id", "article_path", "source_revision", "content_hash",
        "snapshot_json", "expires_at", "created_at",
    },
    "submissions": {
        "id", "owner_id", "workspace_id", "status", "snapshot_json", "content_hash",
        "ai_report_json", "reason", "reviewer_id", "public_entry_id", "created_at", "updated_at",
    },
    "public_entries": {
        "id", "author_id", "status", "current_revision_id", "created_at", "updated_at",
        "moderation_reason", "moderated_by", "moderated_at",
    },
    "public_revisions": {
        "id", "entry_id", "submission_id", "version", "snapshot_json", "content_hash", "published_at",
    },
    "reports": {
        "id", "entry_id", "reporter_id", "reason_code", "detail", "status", "resolution",
        "resolved_by", "created_at", "updated_at",
    },
    "notifications": {
        "id", "user_id", "kind", "object_type", "object_id", "title", "message", "read_at", "created_at",
    },
    "audit_events": {"id", "actor_id", "action", "object_type", "object_id", "detail_json", "created_at"},
    "migrations": {"id", "kind", "workspace_id", "status", "manifest_json", "created_at", "updated_at"},
}
WORKSPACE_SCHEMA_ANCHORS = {
    "idempotency": {"endpoint", "key", "payload_hash", "status", "response_json", "created_at"},
    "tasks": {
        "id", "kind", "subject", "active_key", "status", "payload_json", "result_json",
        "error_type", "error_message", "attempts", "next_run_at", "created_at", "updated_at",
    },
    "raw_records": {
        "path", "byte_hash", "text_hash", "target_path", "disposition", "operation_id", "created_at",
    },
}
PLATFORM_OPTIONAL_SCHEMA = {
    "organizations": {
        "id", "kind", "personal_owner_id", "display_name", "status", "created_by", "created_at", "updated_at",
    },
    "organization_members": {
        "organization_id", "user_id", "role", "status", "added_by", "created_at", "updated_at",
    },
    "workspace_members": {
        "organization_id", "workspace_id", "user_id", "role", "status", "is_default", "added_by",
        "created_at", "updated_at",
    },
    "workspace_invitations": {
        "id", "organization_id", "workspace_id", "invitee_user_id", "role", "status", "invited_by",
        "expires_at", "created_at", "updated_at",
    },
    "platform_idempotency": {
        "scope", "endpoint", "key", "payload_hash", "status", "response_json", "created_at", "updated_at",
    },
    "account_registration_invites": {
        "id", "token_hash", "email", "status", "expires_at", "created_at", "used_at",
    },
    "rate_limits": {"scope_hash", "window_started", "request_count", "updated_at"},
    "public_categories": {
        "id", "slug", "name", "description", "status", "sort_order", "created_by", "created_at", "updated_at",
    },
    "public_tags": {"id", "slug", "name", "status", "created_at", "updated_at"},
    "public_entry_tags": {"entry_id", "tag_id"},
    "public_category_mappings": {"private_label", "category_id", "status", "mapped_by", "updated_at"},
    "public_category_slug_redirects": {"slug", "category_id", "created_at"},
    "public_reuse_permissions": {"entry_id", "permission", "granted_by", "granted_at", "revoked_at", "policy_version"},
    "public_imports": {
        "id", "user_id", "workspace_id", "public_entry_id", "public_revision_id", "private_article_id",
        "private_path", "status", "imported_at", "created_at", "updated_at", "policy_version",
    },
    "public_subscriptions": {"user_id", "public_entry_id", "status", "created_at", "updated_at"},
    "correction_suggestions": {
        "id", "submitter_id", "entry_id", "revision_id", "kind", "detail", "evidence_url", "status",
        "author_response", "resolved_at", "created_at", "updated_at",
    },
    "public_profiles": {"id", "user_id", "display_name", "bio", "status", "created_at", "updated_at"},
    "public_collections": {
        "id", "slug", "title", "description", "status", "curator_id", "published_at", "created_at", "updated_at",
    },
    "public_collection_items": {"collection_id", "entry_id", "sort_order", "curator_note"},
    "curation_records": {
        "id", "object_type", "object_id", "curator_id", "action", "reason", "sort_order", "created_at",
    },
    "public_search_documents": {
        "entry_id", "revision_id", "title", "summary", "body_text", "public_category_id", "category_name",
        "public_tags", "attribution", "first_published_at", "updated_at", "status",
    },
    "public_index_jobs": {"entry_id", "status", "attempts", "last_error", "not_before", "updated_at"},
    "public_search_meta": {"id", "generation"},
    "public_revision_sources": {"revision_id", "position", "label", "url", "kind"},
    "public_revision_reviews": {
        "revision_id", "ai_policy_version", "ai_model", "ai_rules_version", "ai_report_json",
        "admin_reason", "reviewed_by", "reviewed_at",
    },
    "public_revision_taxonomy": {"revision_id", "category_id", "attribution", "change_summary"},
    "public_revision_tags": {"revision_id", "tag_id"},
    "submission_review_attempts": {
        "submission_id", "attempt", "status", "policy_version", "provider", "model", "rules_version",
        "report_json", "started_at", "completed_at",
    },
    "public_search_fts": {"entry_id", "title", "summary", "body_text", "category_name", "public_tags", "attribution"},
}
WORKSPACE_OPTIONAL_SCHEMA = {
    "classification_suggestions": {
        "article_id", "article_revision", "taxonomy_revision", "status", "suggestion_json", "task_id",
        "error_type", "error_message", "created_at", "updated_at",
    },
    "raw_classification_plans": {
        "raw_path", "raw_revision", "taxonomy_revision", "status", "plan_json", "task_id", "error_type",
        "error_message", "created_at", "updated_at",
    },
    "classification_previews": {"id", "kind", "payload_json", "expires_at", "created_at"},
    "classification_draft": {"id", "revision", "payload_json", "updated_at"},
    "reconciliation_items": {
        "id", "fingerprint", "kind", "payload_json", "status", "created_at", "updated_at",
    },
}
PLATFORM_PRIMARY_KEYS = {
    "users": ("id",),
    "workspaces": ("id",),
    "sessions": ("token_hash",),
    "recovery_codes": ("code_hash",),
    "login_attempts": ("scope_hash",),
    "model_settings": ("workspace_id",),
    "share_previews": ("id",),
    "submissions": ("id",),
    "public_entries": ("id",),
    "public_revisions": ("id",),
    "reports": ("id",),
    "notifications": ("id",),
    "audit_events": ("id",),
    "migrations": ("id",),
}
PLATFORM_UNIQUE_KEYS = {
    "users": {("email",)},
    "workspaces": {("root_name",)},
    "public_revisions": {("submission_id",), ("entry_id", "version")},
    "migrations": {("kind",)},
}
WORKSPACE_PRIMARY_KEYS = {
    "idempotency": ("endpoint", "key"),
    "tasks": ("id",),
    "raw_records": ("path",),
}
PLATFORM_OPTIONAL_PRIMARY_KEYS = {
    "organizations": ("id",),
    "organization_members": ("organization_id", "user_id"),
    "workspace_members": ("workspace_id", "user_id"),
    "workspace_invitations": ("id",),
    "platform_idempotency": ("scope", "endpoint", "key"),
    "account_registration_invites": ("id",),
    "rate_limits": ("scope_hash",),
    "public_categories": ("id",),
    "public_tags": ("id",),
    "public_entry_tags": ("entry_id", "tag_id"),
    "public_category_mappings": ("private_label",),
    "public_category_slug_redirects": ("slug",),
    "public_reuse_permissions": ("entry_id",),
    "public_imports": ("id",),
    "public_subscriptions": ("user_id", "public_entry_id"),
    "correction_suggestions": ("id",),
    "public_profiles": ("id",),
    "public_collections": ("id",),
    "public_collection_items": ("collection_id", "entry_id"),
    "curation_records": ("id",),
    "public_search_documents": ("entry_id",),
    "public_index_jobs": ("entry_id",),
    "public_search_meta": ("id",),
    "public_revision_sources": ("revision_id", "position"),
    "public_revision_reviews": ("revision_id",),
    "public_revision_taxonomy": ("revision_id",),
    "public_revision_tags": ("revision_id", "tag_id"),
    "submission_review_attempts": ("submission_id", "attempt"),
    "public_search_fts": (),
}
WORKSPACE_OPTIONAL_PRIMARY_KEYS = {
    "classification_suggestions": ("article_id", "article_revision", "taxonomy_revision"),
    "raw_classification_plans": ("raw_path", "raw_revision", "taxonomy_revision"),
    "classification_previews": ("id",),
    "classification_draft": ("id",),
    "reconciliation_items": ("id",),
}
PLATFORM_OPTIONAL_UNIQUE_KEYS = {
    "organizations": {("personal_owner_id",)},
    "account_registration_invites": {("token_hash",)},
    "public_categories": {("slug",)},
    "public_tags": {("slug",)},
    "public_imports": {("workspace_id", "public_revision_id")},
    "public_profiles": {("user_id",)},
    "public_collections": {("slug",)},
}
WORKSPACE_OPTIONAL_UNIQUE_KEYS = {"reconciliation_items": {("fingerprint",)}}
LEGACY_PLATFORM_IDEMPOTENCY_COLUMNS = {
    "user_id", "endpoint", "key", "payload_hash", "status", "response_json", "created_at", "updated_at",
}
PLATFORM_TEXT_IDENTITY_COLUMNS = {
    "users": {"id", "email"},
    "workspaces": {"id", "owner_id", "root_name"},
    "sessions": {"token_hash", "user_id"},
    "recovery_codes": {"code_hash", "user_id"},
    "login_attempts": {"scope_hash"},
    "model_settings": {"workspace_id"},
    "share_previews": {"id", "owner_id", "workspace_id"},
    "submissions": {"id", "owner_id", "workspace_id"},
    "public_entries": {"id", "author_id"},
    "public_revisions": {"id", "entry_id", "submission_id"},
    "reports": {"id", "entry_id"},
    "notifications": {"id", "user_id"},
    "audit_events": {"id"},
    "migrations": {"id", "kind", "workspace_id"},
    "organizations": {"id", "personal_owner_id"},
    "organization_members": {"organization_id", "user_id"},
    "workspace_members": {"organization_id", "workspace_id", "user_id"},
    "workspace_invitations": {"id", "organization_id", "workspace_id", "invitee_user_id"},
    "platform_idempotency": {"scope", "endpoint", "key"},
    "account_registration_invites": {"id", "token_hash", "email"},
    "rate_limits": {"scope_hash"},
    "public_categories": {"id", "slug", "created_by"},
    "public_tags": {"id", "slug"},
    "public_entry_tags": {"entry_id", "tag_id"},
    "public_category_mappings": {"private_label", "category_id", "mapped_by"},
    "public_category_slug_redirects": {"slug", "category_id"},
    "public_reuse_permissions": {"entry_id", "granted_by"},
    "public_imports": {"id", "user_id", "workspace_id", "public_entry_id", "public_revision_id", "private_article_id"},
    "public_subscriptions": {"user_id", "public_entry_id"},
    "correction_suggestions": {"id", "submitter_id", "entry_id", "revision_id"},
    "public_profiles": {"id", "user_id"},
    "public_collections": {"id", "slug", "curator_id"},
    "public_collection_items": {"collection_id", "entry_id"},
    "curation_records": {"id", "object_id", "curator_id"},
    "public_search_documents": {"entry_id", "revision_id", "public_category_id"},
    "public_index_jobs": {"entry_id"},
    "public_revision_sources": {"revision_id"},
    "public_revision_reviews": {"revision_id", "reviewed_by"},
    "public_revision_taxonomy": {"revision_id", "category_id"},
    "public_revision_tags": {"revision_id", "tag_id"},
    "submission_review_attempts": {"submission_id"},
}
EXPECTED_PUBLIC_SEARCH_FTS_SQL = """CREATE VIRTUAL TABLE public_search_fts USING fts5(
    entry_id UNINDEXED,title,summary,body_text,category_name,public_tags,attribution,
    tokenize='unicode61 remove_diacritics 2'
)"""
WORKSPACE_TEXT_IDENTITY_COLUMNS = {
    "idempotency": {"endpoint", "key"},
    "tasks": {"id", "active_key"},
    "raw_records": {"path"},
    "classification_suggestions": {"article_id", "article_revision"},
    "raw_classification_plans": {"raw_path", "raw_revision"},
    "classification_previews": {"id"},
    "reconciliation_items": {"id", "fingerprint"},
}
PLATFORM_CORRECTNESS_INDEXES = {
    "idx_workspaces_org_identity": {
        "table": "workspaces",
        "columns": ("id", "organization_id"),
        "predicate": None,
        "sql": "CREATE UNIQUE INDEX idx_workspaces_org_identity "
        "ON workspaces(id, organization_id)",
    },
    "idx_workspace_members_default": {
        "table": "workspace_members",
        "columns": ("user_id",),
        "predicate": "is_default=1 AND status='active'",
        "sql": "CREATE UNIQUE INDEX idx_workspace_members_default "
        "ON workspace_members(user_id) WHERE is_default=1 AND status='active'",
    },
    "idx_workspace_invitation_pending": {
        "table": "workspace_invitations",
        "columns": ("workspace_id", "invitee_user_id"),
        "predicate": "status='pending'",
        "sql": "CREATE UNIQUE INDEX idx_workspace_invitation_pending "
        "ON workspace_invitations(workspace_id,invitee_user_id) WHERE status='pending'",
    },
    "idx_public_entries_source_article": {
        "table": "public_entries",
        "columns": ("author_id", "source_workspace_id", "source_article_id"),
        "predicate": "source_workspace_id IS NOT NULL AND source_article_id IS NOT NULL",
        "sql": "CREATE UNIQUE INDEX idx_public_entries_source_article "
        "ON public_entries(author_id,source_workspace_id,source_article_id) "
        "WHERE source_workspace_id IS NOT NULL AND source_article_id IS NOT NULL",
    },
    "idx_public_categories_normalized_name": {
        "table": "public_categories",
        "columns": ("normalized_name",),
        "predicate": None,
        "sql": "CREATE UNIQUE INDEX idx_public_categories_normalized_name "
        "ON public_categories(normalized_name)",
    },
    "idx_public_tags_normalized_name": {
        "table": "public_tags",
        "columns": ("normalized_name",),
        "predicate": None,
        "sql": "CREATE UNIQUE INDEX idx_public_tags_normalized_name "
        "ON public_tags(normalized_name)",
    },
}
SQLITE_ASCII_CASE_TRANSLATION = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz",
)


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


def _sqlite_ascii_fold(value: str) -> str:
    return value.translate(SQLITE_ASCII_CASE_TRANSLATION)


def _sql_tokens(sql: str) -> tuple[tuple[str, str], ...] | None:
    tokens = []
    index = 0
    while index < len(sql):
        character = sql[index]
        if character.isspace():
            index += 1
            continue
        if character == "'":
            index += 1
            value = []
            while index < len(sql):
                if sql[index] == "'":
                    if index + 1 < len(sql) and sql[index + 1] == "'":
                        value.append("'")
                        index += 2
                        continue
                    index += 1
                    break
                value.append(sql[index])
                index += 1
            else:
                return None
            tokens.append(("string", "".join(value)))
            continue
        if character in {'"', "`", "["}:
            closing = "]" if character == "[" else character
            index += 1
            value = []
            while index < len(sql):
                if sql[index] == closing:
                    if index + 1 < len(sql) and sql[index + 1] == closing:
                        value.append(closing)
                        index += 2
                        continue
                    index += 1
                    break
                value.append(sql[index])
                index += 1
            else:
                return None
            tokens.append(("word", _sqlite_ascii_fold("".join(value))))
            continue
        if character.isalpha() or character == "_":
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            tokens.append(("word", _sqlite_ascii_fold(sql[index:end])))
            index = end
            continue
        if character.isdigit():
            end = index + 1
            while end < len(sql) and (sql[end].isdigit() or sql[end] == "."):
                end += 1
            tokens.append(("number", sql[index:end]))
            index = end
            continue
        operator = next(
            (candidate for candidate in ("->>", "==", "!=", "<>", "<=", ">=", "||", "->")
             if sql.startswith(candidate, index)),
            None,
        )
        if operator is not None:
            tokens.append(("symbol", operator))
            index += len(operator)
            continue
        tokens.append(("symbol", character))
        index += 1
    return tuple(tokens)


def _check_platform_correctness_indexes(db: sqlite3.Connection) -> list[str]:
    missing = []
    for name, expected in PLATFORM_CORRECTNESS_INDEXES.items():
        row = db.execute(
            "SELECT type,tbl_name,sql FROM sqlite_schema WHERE name=?", (name,),
        ).fetchone()
        if row is not None:
            if row[0] != "index":
                raise RuntimeError(f"platform SQLite schema has an invalid correctness index: {name}")
            table = str(row[1])
            index_rows = list(db.execute(f'PRAGMA index_list("{table}")'))
            index = next((item for item in index_rows if str(item[1]) == name), None)
            columns = tuple(
                str(item[2]) for item in sorted(
                    db.execute(f'PRAGMA index_info("{name}")'), key=lambda item: item[0],
                )
            )
            expected_partial = expected["predicate"] is not None
            if (
                table != expected["table"]
                or index is None
                or not index[2]
                or bool(index[4]) != expected_partial
                or columns != expected["columns"]
                or not row[2]
                or _sql_tokens(str(row[2])) != _sql_tokens(str(expected["sql"]))
            ):
                raise RuntimeError(f"platform SQLite schema has an invalid correctness index: {name}")
            continue

        table = str(expected["table"])
        schema_type = db.execute(
            "SELECT type,sql FROM sqlite_schema WHERE name=?", (table,),
        ).fetchone()
        if schema_type is None:
            continue
        columns = {str(item[1]) for item in db.execute(f'PRAGMA table_info("{table}")')}
        if not set(expected["columns"]).issubset(columns):
            continue
        missing.append(expected["sql"])

    return missing


def _stored_taxonomy_item(db: sqlite3.Connection, item: object, kind: str) -> dict:
    if not isinstance(item, dict):
        raise RuntimeError("platform SQLite has invalid taxonomy proposal data")
    table = "public_categories" if kind == "category" else "public_tags"
    if item.get("kind") == "existing" and set(item) == {"kind", "id", "name"}:
        item_id = item.get("id")
        try:
            taxonomy_normalized_key(item.get("name"))
        except ValueError as exc:
            raise RuntimeError("platform SQLite has invalid taxonomy proposal data") from exc
        if (
            not isinstance(item_id, str)
            or not re.fullmatch(r"[a-f0-9]{32}", item_id)
            or db.execute(f"SELECT 1 FROM {table} WHERE id=?", (item_id,)).fetchone() is None
        ):
            raise RuntimeError("platform SQLite has invalid taxonomy proposal data")
        return item
    if item.get("kind") == "proposal" and set(item) == {"kind", "key", "name", "normalized_name"}:
        try:
            clean_name, normalized = normalize_taxonomy_name(item.get("name"), kind=kind)
        except ValueError as exc:
            raise RuntimeError("platform SQLite has invalid taxonomy proposal data") from exc
        expected_key = hashlib.sha256(f"{kind}:{normalized}".encode()).hexdigest()[:20]
        if item.get("name") != clean_name or item.get("normalized_name") != normalized or item.get("key") != expected_key:
            raise RuntimeError("platform SQLite has invalid taxonomy proposal data")
        return item
    raise RuntimeError("platform SQLite has invalid taxonomy proposal data")


def _stored_taxonomy_proposal(db: sqlite3.Connection, value: object) -> dict:
    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise RuntimeError("platform SQLite has invalid taxonomy proposal data") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "category", "tags"} or payload.get("version") != 1:
        raise RuntimeError("platform SQLite has invalid taxonomy proposal data")
    category = _stored_taxonomy_item(db, payload.get("category"), "category")
    raw_tags = payload.get("tags")
    if not isinstance(raw_tags, list) or len(raw_tags) > 3:
        raise RuntimeError("platform SQLite has invalid taxonomy proposal data")
    tags = [_stored_taxonomy_item(db, item, "tag") for item in raw_tags]
    identities = [item.get("id") or item.get("normalized_name") for item in tags]
    if len(identities) != len(set(identities)):
        raise RuntimeError("platform SQLite has invalid taxonomy proposal data")
    return {"version": 1, "category": category, "tags": tags}


def _check_taxonomy_storage(db: sqlite3.Connection) -> None:
    for table in ("public_categories", "public_tags"):
        columns = {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}
        if not columns:
            continue
        rows = db.execute(f"SELECT id,name{',normalized_name' if 'normalized_name' in columns else ''} FROM {table}").fetchall()
        seen: dict[str, str] = {}
        for row in rows:
            try:
                normalized = taxonomy_normalized_key(row["name"])
            except ValueError as exc:
                raise RuntimeError("platform SQLite has invalid public taxonomy names") from exc
            if normalized in seen and seen[normalized] != row["id"]:
                raise RuntimeError("platform SQLite has duplicate normalized public taxonomy names")
            seen[normalized] = row["id"]
            if "normalized_name" in columns and (
                not isinstance(row["normalized_name"], str) or row["normalized_name"] != normalized
            ):
                raise RuntimeError("platform SQLite has invalid normalized public taxonomy names")

    proposals: dict[str, dict] = {}
    for table in ("share_previews", "submissions"):
        columns = {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}
        if "taxonomy_proposal_json" not in columns:
            continue
        for row in db.execute(f"SELECT id,taxonomy_proposal_json FROM {table} WHERE taxonomy_proposal_json IS NOT NULL"):
            proposal = _stored_taxonomy_proposal(db, row["taxonomy_proposal_json"])
            if table == "submissions":
                proposals[row["id"]] = proposal

    submission_columns = {str(row[1]) for row in db.execute('PRAGMA table_info("submissions")')}
    if "taxonomy_decision_json" not in submission_columns:
        return
    for row in db.execute("SELECT id,status,public_entry_id,taxonomy_decision_json FROM submissions"):
        proposal = proposals.get(row["id"])
        decision_json = row["taxonomy_decision_json"]
        if row["status"] != "approved":
            if decision_json is not None:
                raise RuntimeError("platform SQLite has inconsistent taxonomy decision data")
            continue
        if proposal is None:
            if decision_json is not None:
                raise RuntimeError("platform SQLite has invalid taxonomy decision data")
            continue
        if decision_json is None:
            raise RuntimeError("platform SQLite has inconsistent taxonomy decision data")
        try:
            decision = json.loads(decision_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("platform SQLite has invalid taxonomy decision data") from exc
        if not isinstance(decision, dict) or set(decision) != {"version", "resolutions"} or decision.get("version") != 1:
            raise RuntimeError("platform SQLite has invalid taxonomy decision data")
        expected_items = [proposal["category"], *proposal["tags"]]
        resolutions = decision.get("resolutions")
        if not isinstance(resolutions, list) or len(resolutions) != len(expected_items):
            raise RuntimeError("platform SQLite has invalid taxonomy decision data")
        resolved_ids: list[str] = []
        for item, resolution, kind in zip(
            expected_items,
            resolutions,
            ["category", *("tag" for _ in proposal["tags"])],
        ):
            if not isinstance(resolution, dict) or resolution.get("kind") != kind:
                raise RuntimeError("platform SQLite has invalid taxonomy decision data")
            expected_keys = {"kind", "action", "id"} if item["kind"] == "existing" else {"kind", "action", "key", "id"}
            expected_actions = {"accept"} if item["kind"] == "existing" else {"create", "map", "reuse"}
            table = "public_categories" if kind == "category" else "public_tags"
            target = db.execute(
                f"SELECT id,normalized_name FROM {table} WHERE id=?", (resolution.get("id"),),
            ).fetchone()
            if (
                set(resolution) != expected_keys
                or resolution.get("action") not in expected_actions
                or (item["kind"] == "proposal" and resolution.get("key") != item["key"])
                or not isinstance(resolution.get("id"), str)
                or target is None
                or (item["kind"] == "existing" and resolution.get("id") != item["id"])
                or (
                    item["kind"] == "proposal"
                    and resolution.get("action") in {"create", "reuse"}
                    and target["normalized_name"] != item["normalized_name"]
                )
            ):
                raise RuntimeError("platform SQLite has invalid taxonomy decision data")
            resolved_ids.append(resolution["id"])

        revisions = db.execute(
            "SELECT id,entry_id FROM public_revisions WHERE submission_id=?",
            (row["id"],),
        ).fetchall()
        if len(revisions) != 1 or revisions[0]["entry_id"] != row["public_entry_id"]:
            raise RuntimeError("platform SQLite has inconsistent taxonomy decision data")
        revision_id = revisions[0]["id"]
        frozen_category = db.execute(
            "SELECT category_id FROM public_revision_taxonomy WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        frozen_tags = db.execute(
            "SELECT tag_id FROM public_revision_tags WHERE revision_id=? ORDER BY rowid",
            (revision_id,),
        ).fetchall()
        frozen_ids = [
            frozen_category["category_id"] if frozen_category is not None else None,
            *(tag["tag_id"] for tag in frozen_tags),
        ]
        resolved_relation_ids = [resolved_ids[0], *dict.fromkeys(resolved_ids[1:])]
        if resolved_relation_ids != frozen_ids:
            raise RuntimeError("platform SQLite has inconsistent taxonomy decision data")


def _check_application_schema(db: sqlite3.Connection, path: Path) -> list[str]:
    db.row_factory = sqlite3.Row
    anchors = PLATFORM_SCHEMA_ANCHORS if path.parent.name == ".platform" else WORKSPACE_SCHEMA_ANCHORS
    primary_keys = PLATFORM_PRIMARY_KEYS if anchors is PLATFORM_SCHEMA_ANCHORS else WORKSPACE_PRIMARY_KEYS
    unique_keys = PLATFORM_UNIQUE_KEYS if anchors is PLATFORM_SCHEMA_ANCHORS else {}
    optional = PLATFORM_OPTIONAL_SCHEMA if anchors is PLATFORM_SCHEMA_ANCHORS else WORKSPACE_OPTIONAL_SCHEMA
    optional_primary = (
        PLATFORM_OPTIONAL_PRIMARY_KEYS if anchors is PLATFORM_SCHEMA_ANCHORS else WORKSPACE_OPTIONAL_PRIMARY_KEYS
    )
    optional_unique = (
        PLATFORM_OPTIONAL_UNIQUE_KEYS if anchors is PLATFORM_SCHEMA_ANCHORS else WORKSPACE_OPTIONAL_UNIQUE_KEYS
    )
    text_columns = (
        PLATFORM_TEXT_IDENTITY_COLUMNS if anchors is PLATFORM_SCHEMA_ANCHORS else WORKSPACE_TEXT_IDENTITY_COLUMNS
    )
    label = "platform" if anchors is PLATFORM_SCHEMA_ANCHORS else "workspace state"
    if db.execute("SELECT 1 FROM sqlite_schema WHERE type='trigger' LIMIT 1").fetchone() is not None:
        raise RuntimeError(f"{label} SQLite schema contains unsupported triggers")
    if anchors is PLATFORM_SCHEMA_ANCHORS:
        tables_with_session_foreign_keys = []
        for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            table = str(row[0])
            if any(
                str(foreign_key[2]).casefold() == "sessions"
                for foreign_key in db.execute(f'PRAGMA foreign_key_list("{table}")')
            ):
                tables_with_session_foreign_keys.append(table)
        if tables_with_session_foreign_keys:
            raise RuntimeError("platform SQLite schema contains unsupported session references")
    tables = [(table, columns, True) for table, columns in anchors.items()]
    tables.extend((table, columns, False) for table, columns in optional.items())
    for table, required_columns, required in tables:
        schema_type = db.execute(
            "SELECT type,sql FROM sqlite_schema WHERE name=?", (table,),
        ).fetchone()
        if schema_type is None and not required:
            continue
        if schema_type is None or schema_type[0] != "table":
            raise RuntimeError(f"{label} SQLite schema is unsupported: {table}")
        if table == "public_search_fts" and (
            not schema_type[1]
            or _sql_tokens(str(schema_type[1])) != _sql_tokens(EXPECTED_PUBLIC_SEARCH_FTS_SQL)
        ):
            raise RuntimeError("platform SQLite schema has an invalid public search virtual table")
        table_info = list(db.execute(f'PRAGMA table_info("{table}")'))
        columns = {str(row[1]) for row in table_info}
        legacy_platform_idempotency = table == "platform_idempotency" and "scope" not in columns
        if legacy_platform_idempotency:
            required_columns = LEGACY_PLATFORM_IDEMPOTENCY_COLUMNS
        if not required_columns.issubset(columns):
            raise RuntimeError(f"{label} SQLite schema is unsupported: {table}")
        declared_types = {str(row[1]): str(row[2]).upper() for row in table_info}
        required_text_columns = text_columns.get(table, set())
        if legacy_platform_idempotency:
            required_text_columns = {"user_id", "endpoint", "key"}
        for column in required_text_columns:
            if declared_types.get(column) != "TEXT":
                raise RuntimeError(f"{label} SQLite schema has an unsupported identity type: {table}")
        primary_key = tuple(
            str(row[1]) for row in sorted((row for row in table_info if row[5]), key=lambda row: row[5])
        )
        expected_primary_key = primary_keys.get(table, optional_primary.get(table))
        if legacy_platform_idempotency:
            expected_primary_key = ("user_id", "endpoint", "key")
        if primary_key != expected_primary_key:
            raise RuntimeError(f"{label} SQLite schema has an unsupported primary key: {table}")
        actual_unique_keys = set()
        for index in db.execute(f'PRAGMA index_list("{table}")'):
            if not index[2] or index[4]:
                continue
            columns = tuple(
                str(row[2]) for row in sorted(
                    db.execute(f'PRAGMA index_info("{index[1]}")'), key=lambda row: row[0],
                )
            )
            actual_unique_keys.add(columns)
        expected_unique_keys = unique_keys.get(table, optional_unique.get(table, set()))
        if not expected_unique_keys.issubset(actual_unique_keys):
            raise RuntimeError(f"{label} SQLite schema is missing a unique constraint: {table}")
    if anchors is PLATFORM_SCHEMA_ANCHORS:
        _check_taxonomy_storage(db)
        meta_table = db.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='public_search_meta'",
        ).fetchone()
        if meta_table is not None:
            meta = db.execute(
                "SELECT id,generation,typeof(generation) value_type FROM public_search_meta WHERE id=1",
            ).fetchone()
            if (
                meta is None or meta[0] != 1 or meta[2] != "integer" or int(meta[1]) < 0
                or db.execute("SELECT COUNT(*) FROM public_search_meta").fetchone()[0] != 1
            ):
                raise RuntimeError("platform SQLite schema has invalid public search metadata")
        taxonomy_tables = db.execute("""
            SELECT COUNT(*) FROM sqlite_schema
            WHERE type='table' AND name IN ('public_categories','public_category_slug_redirects')
        """).fetchone()[0]
        if taxonomy_tables == 2 and db.execute("""
            SELECT 1 FROM public_categories category
            JOIN public_category_slug_redirects redirect ON redirect.slug=category.slug
            WHERE redirect.category_id<>category.id AND category.status<>'merged' LIMIT 1
        """).fetchone() is not None:
            raise RuntimeError("platform SQLite has a conflicting public category slug namespace")
        jobs_table = db.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='public_index_jobs'",
        ).fetchone()
        if jobs_table is not None:
            canonical_utc = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00")
            for row in db.execute(
                "SELECT status,attempts,typeof(attempts),not_before,updated_at FROM public_index_jobs",
            ):
                try:
                    if row[0] not in {"pending", "running", "retry", "dead"}:
                        raise ValueError
                    if row[2] != "integer" or int(row[1]) < 0:
                        raise ValueError
                    if row[0] == "running" and (int(row[1]) < 1 or row[3] is not None):
                        raise ValueError
                    if row[3] is not None:
                        not_before_text = str(row[3])
                        not_before = datetime.fromisoformat(not_before_text)
                        if not canonical_utc.fullmatch(not_before_text) or not_before.utcoffset() != timezone.utc.utcoffset(None):
                            raise ValueError
                    updated_at_text = str(row[4])
                    updated_at = datetime.fromisoformat(updated_at_text)
                    if not canonical_utc.fullmatch(updated_at_text) or updated_at.utcoffset() != timezone.utc.utcoffset(None):
                        raise ValueError
                except (TypeError, ValueError):
                    raise RuntimeError("platform SQLite has invalid public index jobs") from None
        return _check_platform_correctness_indexes(db)
    return []


def _exercise_session_revocation(db: sqlite3.Connection, *, commit: bool) -> None:
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("BEGIN IMMEDIATE")
    try:
        session_count = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        changes_before = db.total_changes
        db.execute("DELETE FROM sessions")
        if (
            db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] != 0
            or db.total_changes - changes_before != session_count
        ):
            raise RuntimeError("platform SQLite cannot revoke restored sessions")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("platform SQLite cannot revoke restored sessions")
        if commit:
            db.commit()
        else:
            db.rollback()
    except BaseException:
        db.rollback()
        raise


def _check_platform_restore_actions(db: sqlite3.Connection, path: Path) -> None:
    if path.parent.name != ".platform":
        return
    probe = sqlite3.connect(":memory:")
    try:
        db.backup(probe)
        _exercise_session_revocation(probe, commit=False)
    except (RuntimeError, sqlite3.DatabaseError) as exc:
        raise RuntimeError("platform SQLite cannot revoke restored sessions") from exc
    finally:
        probe.close()


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
        missing_indexes = _check_application_schema(db, path)
        checked_db = db
        probe = None
        if missing_indexes:
            probe = sqlite3.connect(":memory:")
            try:
                db.backup(probe)
                for statement in missing_indexes:
                    probe.execute(statement)
            except sqlite3.DatabaseError as exc:
                probe.close()
                raise RuntimeError("platform SQLite cannot create required correctness indexes") from exc
            checked_db = probe
        try:
            if checked_db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError(f"SQLite foreign key check failed: {path.name}")
            _check_platform_restore_actions(checked_db, path)
        finally:
            if probe is not None:
                probe.close()
    if checkpoint:
        wal = Path(f"{path}-wal")
        if wal.exists() and wal.stat().st_size:
            raise RuntimeError(f"SQLite WAL was not fully checkpointed: {path.name}")


def _copy_source_data(source: Path, destination: Path) -> None:
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
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            if _is_sqlite_sidecar(root, path):
                continue
            raise RuntimeError(f"{label} changed during validation")
        if stat.S_ISLNK(mode):
            raise RuntimeError(f"{label} must not contain symbolic links")
        if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
            raise RuntimeError(f"{label} contains an unsupported file type")


def _check_workspace_layout(root: Path) -> None:
    platform_db = root / ".platform" / "platform.sqlite3"
    target = f"{platform_db.resolve().as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(target, uri=True) as db:
        columns = {str(row[1]) for row in db.execute('PRAGMA table_info("workspaces")')}
        query = "SELECT root_name,0 AS retired_personal FROM workspaces"
        organization_table = db.execute(
            "SELECT type FROM sqlite_schema WHERE name='organizations'",
        ).fetchone()
        if "organization_id" in columns and organization_table and organization_table[0] == "table":
            query = """
                SELECT workspace.root_name,
                    CASE WHEN (
                        organization.kind='personal'
                        AND organization.status='deleted'
                        AND personal_owner.id IS NOT NULL
                        AND personal_owner.status='deleted'
                        AND NOT EXISTS (
                            SELECT 1 FROM workspace_members member
                            WHERE member.workspace_id=workspace.id AND member.status='active'
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM organization_members member
                            WHERE member.organization_id=organization.id AND member.status='active'
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM model_settings setting WHERE setting.workspace_id=workspace.id
                        )
                    ) THEN 1 ELSE 0 END AS retired_personal
                FROM workspaces workspace
                LEFT JOIN organizations organization ON organization.id=workspace.organization_id
                LEFT JOIN users personal_owner ON personal_owner.id=organization.personal_owner_id
            """
        rows = db.execute(query).fetchall()
    required_roots = set()
    for root_name, retired_personal in rows:
        if (
            not isinstance(root_name, str)
            or not root_name
            or Path(root_name).parts != (root_name,)
            or root_name in {".", ".."}
        ):
            raise RuntimeError("platform workspace root is invalid")
        if not retired_personal:
            required_roots.add(root_name)
    spaces = root / "spaces"
    actual_roots = set()
    for workspace in spaces.iterdir():
        if workspace.is_symlink() or not workspace.is_dir():
            raise RuntimeError("backup spaces contains unexpected entries")
        actual_roots.add(workspace.name)
    if actual_roots - required_roots:
        raise RuntimeError("backup spaces contains retired or orphan workspace roots")
    if required_roots - actual_roots:
        raise RuntimeError("backup workspace layout is incomplete")
    for root_name in required_roots:
        workspace = root / "spaces" / root_name
        if any(path.is_symlink() or not path.is_dir() for path in (workspace, workspace / "wiki", workspace / "raw")):
            raise RuntimeError(f"backup workspace layout is incomplete: {root_name}")


def _manifest(root: Path, source_root: Path) -> dict:
    for name in DATA_DIRECTORIES:
        _validate_tree(root / name, "backup data")
    entries = []
    directories = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            directories.append(path.relative_to(root).as_posix())
        elif path.is_file() and path != root / MANIFEST_NAME:
            if _is_sqlite_sidecar(root, path):
                raise RuntimeError("published backup must not contain SQLite sidecars")
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
        "directories": directories,
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

    try:
        shutil.copytree(source, temporary, symlinks=True)
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
    source_directories = {
        path.relative_to(source).as_posix()
        for path in source.rglob("*") if path.is_dir() and not path.is_symlink()
    }
    target_directories = {
        path.relative_to(target).as_posix()
        for path in target.rglob("*") if path.is_dir() and not path.is_symlink()
    }
    if source_directories != target_directories:
        return False
    source_files = {
        path.relative_to(source).as_posix(): path
        for path in source.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    target_files = {
        path.relative_to(target).as_posix(): path
        for path in target.rglob("*")
        if path.is_file()
        and not path.is_symlink()
    }
    if set(source_files) != set(target_files):
        return False
    return all(
        source_files[name].stat().st_size == target_files[name].stat().st_size
        and _sha256(source_files[name]) == _sha256(target_files[name])
        for name in source_files
    )


def _prepare_restore_stage(
    backup_root: Path, project_root: Path, stage: Path, verified_manifest_sha256: str,
) -> dict:
    if _sha256(backup_root / MANIFEST_NAME) != verified_manifest_sha256:
        raise RuntimeError("backup changed after verification")
    shutil.copytree(backup_root, stage, symlinks=True)
    if (
        _sha256(backup_root / MANIFEST_NAME) != verified_manifest_sha256
        or _sha256(stage / MANIFEST_NAME) != verified_manifest_sha256
    ):
        raise RuntimeError("backup changed during restore staging")
    # Verify the private copy before applying the documented session revocation.
    verify_backup(stage)
    platform_db = stage / ".platform" / "platform.sqlite3"
    with sqlite3.connect(platform_db) as db:
        for statement in _check_platform_correctness_indexes(db):
            db.execute(statement)
        db.commit()
        _exercise_session_revocation(db, commit=True)
    sqlite_paths = _sqlite_paths(stage)
    for path in sqlite_paths:
        _check_sqlite(path, checkpoint=True)
    for path in sqlite_paths:
        for suffix in ("-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
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
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or not isinstance(manifest.get("files"), list)
        or not isinstance(manifest.get("directories"), list)
    ):
        raise RuntimeError("backup manifest is unsupported")
    expected_directories = set()
    for raw_directory in manifest["directories"]:
        relative = Path(raw_directory) if isinstance(raw_directory, str) else None
        if (
            relative is None
            or relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or relative.parts[0] not in DATA_DIRECTORIES
        ):
            raise RuntimeError("backup manifest contains an invalid directory entry")
        expected_directories.add(raw_directory)
    if len(expected_directories) != len(manifest["directories"]):
        raise RuntimeError("backup manifest contains duplicate directory entries")
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
    for path in backup_root.rglob("*"):
        if path.is_file() and _is_sqlite_sidecar(backup_root, path):
            raise RuntimeError("published backup must not contain SQLite sidecars")
    actual = {
        path.relative_to(backup_root).as_posix(): path
        for path in backup_root.rglob("*")
        if path.is_file()
        and path != manifest_path
    }
    if set(actual) != set(expected):
        raise RuntimeError("backup file set does not match manifest")
    for relative, path in actual.items():
        item = expected[relative]
        if path.is_symlink() or path.stat().st_size != item["size"] or _sha256(path) != item["sha256"]:
            raise RuntimeError(f"backup checksum mismatch: {relative}")
    actual_directories = {
        path.relative_to(backup_root).as_posix()
        for path in backup_root.rglob("*") if path.is_dir() and not path.is_symlink()
    }
    if actual_directories != expected_directories:
        raise RuntimeError("backup directory set does not match manifest")
    key = backup_root / ".platform" / "master.key"
    if not key.is_file() or key.stat().st_size != 32:
        raise RuntimeError("backup platform master key is missing or invalid")
    for path in _sqlite_paths(backup_root):
        _check_sqlite(path, checkpoint=False)
    _check_workspace_layout(backup_root)
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
        _check_workspace_layout(project_root)
        try:
            stage.mkdir(mode=0o700, parents=True)
            _copy_source_data(project_root, stage)
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
    if backup_root.is_symlink():
        raise RuntimeError("backup root must not be a symbolic link")
    backup_root = backup_root.resolve()
    project_root = project_root.resolve()
    manifest_sha256_before = _sha256(backup_root / MANIFEST_NAME)
    manifest = verify_backup(backup_root)
    verified_manifest_sha256 = _sha256(backup_root / MANIFEST_NAME)
    if manifest_sha256_before != verified_manifest_sha256:
        raise RuntimeError("backup changed during verification")
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
                manifest = _prepare_restore_stage(
                    backup_root, project_root, stage, verified_manifest_sha256,
                )
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
                manifest = _prepare_restore_stage(
                    backup_root, project_root, stage, verified_manifest_sha256,
                )
                journal = {
                    "schema_version": RESTORE_JOURNAL_SCHEMA,
                    "backup_manifest_sha256": verified_manifest_sha256,
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
