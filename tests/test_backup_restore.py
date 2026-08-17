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
from state_store import StateStore


LEGACY_PLATFORM_DDL = """
CREATE TABLE users (
    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, nickname TEXT NOT NULL,
    password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','admin')),
    status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
);
CREATE TABLE workspaces (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL UNIQUE REFERENCES users(id),
    root_name TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_hash TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE recovery_codes (
    code_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL, used_at TEXT
);
CREATE TABLE login_attempts (
    scope_hash TEXT PRIMARY KEY, failures INTEGER NOT NULL, blocked_until TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE model_settings (
    workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    provider TEXT NOT NULL, base_url_enc TEXT NOT NULL, api_key_enc TEXT NOT NULL,
    model TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE share_previews (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id), article_path TEXT NOT NULL,
    source_revision TEXT NOT NULL, content_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL,
    expires_at TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE submissions (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id), status TEXT NOT NULL,
    snapshot_json TEXT NOT NULL, content_hash TEXT NOT NULL, ai_report_json TEXT,
    reason TEXT, reviewer_id TEXT REFERENCES users(id), public_entry_id TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE public_entries (
    id TEXT PRIMARY KEY, author_id TEXT NOT NULL REFERENCES users(id), status TEXT NOT NULL,
    current_revision_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    moderation_reason TEXT, moderated_by TEXT REFERENCES users(id), moderated_at TEXT
);
CREATE TABLE public_revisions (
    id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES public_entries(id),
    submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id), version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL, content_hash TEXT NOT NULL, published_at TEXT NOT NULL,
    UNIQUE(entry_id, version)
);
CREATE TABLE reports (
    id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES public_entries(id),
    reporter_id TEXT REFERENCES users(id), reason_code TEXT NOT NULL, detail TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', resolution TEXT, resolved_by TEXT REFERENCES users(id),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE notifications (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT NOT NULL,
    title TEXT NOT NULL, message TEXT NOT NULL, read_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE audit_events (
    id TEXT PRIMARY KEY, actor_id TEXT, action TEXT NOT NULL, object_type TEXT NOT NULL,
    object_id TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE migrations (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL UNIQUE, workspace_id TEXT NOT NULL,
    status TEXT NOT NULL, manifest_json TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

LEGACY_WORKSPACE_DDL = """
CREATE TABLE idempotency (
    endpoint TEXT NOT NULL, key TEXT NOT NULL, payload_hash TEXT NOT NULL,
    status TEXT NOT NULL, response_json TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY(endpoint, key)
);
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, active_key TEXT NOT NULL,
    status TEXT NOT NULL, payload_json TEXT NOT NULL, result_json TEXT, error_type TEXT,
    error_message TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_run_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE raw_records (
    path TEXT PRIMARY KEY, byte_hash TEXT NOT NULL, text_hash TEXT NOT NULL,
    target_path TEXT, disposition TEXT NOT NULL, operation_id TEXT, created_at TEXT NOT NULL
);
"""


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
    state = StateStore(workspace, recover_running=False)
    db = state.connect()
    try:
        db.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        db.execute("INSERT INTO marker VALUES('complete')")
        db.commit()
    finally:
        db.close()
    platform.save_model(
        user["workspace_id"], "openai-compatible", "https://models.example/v1", "secret-key", "model-a",
    )
    platform.create_session(user["id"])
    return platform, user, article, raw


def seed_minimum_legacy_instance(root: Path) -> str:
    platform_root = root / ".platform"
    spaces_root = root / "spaces"
    platform_root.mkdir()
    spaces_root.mkdir()
    (platform_root / "master.key").write_bytes(b"k" * 32)
    database = platform_root / "platform.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript(LEGACY_PLATFORM_DDL)
        db.execute(
            "INSERT INTO users(id,email,nickname,password_hash,role,status,created_at) VALUES(?,?,?,?,?,?,?)",
            ("b" * 32, "legacy@example.com", "Legacy", "unused", "admin", "active", "2026-01-01T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO workspaces(id,owner_id,root_name,display_name,created_at) VALUES(?,?,?,?,?)",
            ("a" * 32, "b" * 32, "a" * 32, "Legacy Wiki", "2026-01-01T00:00:00Z"),
        )
    workspace = spaces_root / ("a" * 32)
    (workspace / "wiki").mkdir(parents=True)
    (workspace / "raw").mkdir()
    state_root = workspace / ".wiki-state"
    state_root.mkdir()
    with sqlite3.connect(state_root / "state.sqlite3") as db:
        db.executescript(LEGACY_WORKSPACE_DDL)
    return "a" * 32


def write_manifest(root: Path) -> None:
    manifest = backup_restore._manifest(root, root)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def mark_deleted_personal_tombstone(platform: PlatformStore, user: dict) -> None:
    with platform.connect() as db:
        organization_id = db.execute(
            "SELECT organization_id FROM workspaces WHERE id=?", (user["workspace_id"],),
        ).fetchone()[0]
        db.execute("UPDATE users SET status='deleted' WHERE id=?", (user["id"],))
        db.execute("UPDATE organizations SET status='deleted' WHERE id=?", (organization_id,))
        db.execute("DELETE FROM workspace_members WHERE workspace_id=?", (user["workspace_id"],))
        db.execute("DELETE FROM organization_members WHERE organization_id=?", (organization_id,))
        db.execute("DELETE FROM model_settings WHERE workspace_id=?", (user["workspace_id"],))


def install_session_delete_blocker(database: Path, blocker: str) -> None:
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA foreign_keys=ON")
        if blocker == "trigger_without_sessions":
            db.execute("DELETE FROM sessions")
        if blocker.startswith("trigger"):
            db.execute("""
                CREATE TRIGGER block_session_delete BEFORE DELETE ON sessions
                BEGIN SELECT RAISE(ABORT,'blocked session cleanup'); END
            """)
        else:
            action = "RESTRICT" if blocker == "foreign_key_restrict" else "CASCADE"
            target = "Sessions" if blocker == "foreign_key_mixed_case_cascade" else "sessions"
            db.execute(f"""
                CREATE TABLE session_references (
                    token_hash TEXT NOT NULL REFERENCES {target}(token_hash) ON DELETE {action}
                )
            """)
            token_hash = db.execute("SELECT token_hash FROM sessions LIMIT 1").fetchone()[0]
            db.execute("INSERT INTO session_references VALUES(?)", (token_hash,))
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    Path(f"{database}-wal").unlink(missing_ok=True)
    Path(f"{database}-shm").unlink(missing_ok=True)


def assert_backup_contract_rejects(source: Path, tmp_path: Path, match: str) -> None:
    with pytest.raises((RuntimeError, sqlite3.DatabaseError), match=match):
        create_backup(source, tmp_path / "invalid-source-backup")

    forged = tmp_path / "forged-backup"
    forged.mkdir()
    backup_restore._copy_source_data(source, forged)
    write_manifest(forged)
    with pytest.raises((RuntimeError, sqlite3.DatabaseError), match=match):
        verify_backup(forged)
    target = tmp_path / "target"
    with pytest.raises((RuntimeError, sqlite3.DatabaseError), match=match):
        restore_backup(forged, target)
    assert not (target / ".platform").exists()
    assert not (target / "spaces").exists()


def assert_correctness_index(db: sqlite3.Connection, name: str) -> None:
    expected = backup_restore.PLATFORM_CORRECTNESS_INDEXES[name]
    row = db.execute(
        "SELECT tbl_name,sql FROM sqlite_schema WHERE type='index' AND name=?", (name,),
    ).fetchone()
    assert row is not None
    index = next(item for item in db.execute(f'PRAGMA index_list("{row[0]}")') if item[1] == name)
    columns = tuple(item[2] for item in db.execute(f'PRAGMA index_info("{name}")'))
    assert row[0] == expected["table"]
    assert index[2] == 1
    assert bool(index[4]) == (expected["predicate"] is not None)
    assert columns == expected["columns"]
    assert backup_restore._sql_tokens(row[1]) == backup_restore._sql_tokens(expected["sql"])


def test_backup_verify_and_restore_round_trip(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _platform, user, article, raw = seed_instance(source)
    backup = tmp_path / "backup-001"

    manifest = create_backup(source, backup)
    assert manifest["schema_version"] == 2
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


def test_backup_restores_minimum_supported_legacy_schemas(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    root_name = seed_minimum_legacy_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    assert verify_backup(backup)["schema_version"] == 2

    target = tmp_path / "target"
    restore_backup(backup, target)
    platform = PlatformStore(target)
    workspace = platform.workspace_root(root_name)
    state = StateStore(workspace, recover_running=False)
    with platform.connect() as db:
        assert "organization_id" in {row["name"] for row in db.execute("PRAGMA table_info(workspaces)")}
        assert db.execute("SELECT COUNT(*) FROM workspace_members").fetchone()[0] == 1
    with state.connect() as db:
        task_columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
        assert {"actor_user_id", "paused_from_status"}.issubset(task_columns)


@pytest.mark.parametrize(
    ("database_kind", "ddl_replacement"),
    [
        ("platform", ("email TEXT NOT NULL UNIQUE", "email TEXT NOT NULL")),
        (
            "workspace",
            ("created_at TEXT NOT NULL,\n    PRIMARY KEY(endpoint, key)", "created_at TEXT NOT NULL"),
        ),
    ],
)
def test_backup_rejects_application_schema_without_identity_constraints(
    tmp_path: Path, database_kind: str, ddl_replacement: tuple[str, str],
):
    source = tmp_path / "source"
    source.mkdir()
    root_name = seed_minimum_legacy_instance(source)
    database = (
        source / ".platform" / "platform.sqlite3"
        if database_kind == "platform"
        else source / "spaces" / root_name / ".wiki-state" / "state.sqlite3"
    )
    database.unlink()
    ddl = LEGACY_PLATFORM_DDL if database_kind == "platform" else LEGACY_WORKSPACE_DDL
    with sqlite3.connect(database) as db:
        db.executescript(ddl.replace(*ddl_replacement))
    with pytest.raises(RuntimeError, match="unique constraint|primary key"):
        create_backup(source, tmp_path / "invalid-backup")

    forged = tmp_path / "forged"
    forged.mkdir()
    for name in backup_restore.DATA_DIRECTORIES:
        backup_restore.shutil.copytree(source / name, forged / name)
    write_manifest(forged)
    with pytest.raises(RuntimeError, match="unique constraint|primary key"):
        verify_backup(forged)


@pytest.mark.parametrize("invalid_kind", ["partial_unique", "identity_type"])
def test_backup_rejects_partial_unique_and_invalid_identity_types(
    tmp_path: Path, invalid_kind: str,
):
    source = tmp_path / "source"
    source.mkdir()
    platform_root = source / ".platform"
    spaces_root = source / "spaces"
    platform_root.mkdir()
    spaces_root.mkdir()
    (platform_root / "master.key").write_bytes(b"k" * 32)
    ddl = LEGACY_PLATFORM_DDL
    suffix = ""
    if invalid_kind == "partial_unique":
        ddl = ddl.replace("email TEXT NOT NULL UNIQUE", "email TEXT NOT NULL")
        suffix = "CREATE UNIQUE INDEX users_email_partial ON users(email) WHERE status='deleted';"
    else:
        ddl = ddl.replace("id TEXT PRIMARY KEY, email", "id INTEGER PRIMARY KEY, email", 1)
    with sqlite3.connect(platform_root / "platform.sqlite3") as db:
        db.executescript(ddl + suffix)
    with pytest.raises(RuntimeError, match="unique constraint|identity type"):
        create_backup(source, tmp_path / "invalid-backup")


@pytest.mark.parametrize("database_kind", ["platform", "workspace"])
def test_backup_rejects_malformed_optional_application_tables(tmp_path: Path, database_kind: str):
    source = tmp_path / "source"
    source.mkdir()
    platform, user, _article, _raw = seed_instance(source)
    if database_kind == "platform":
        with platform.connect() as db:
            db.execute("DROP TABLE workspace_invitations")
            db.execute("CREATE TABLE workspace_invitations(dummy TEXT)")
    else:
        state_path = (
            source / "spaces" / user["workspace_root_name"] / ".wiki-state" / "state.sqlite3"
        )
        with sqlite3.connect(state_path) as db:
            db.execute("DROP TABLE classification_suggestions")
            db.execute("CREATE TABLE classification_suggestions(dummy TEXT)")
    with pytest.raises(RuntimeError, match="SQLite schema is unsupported"):
        create_backup(source, tmp_path / "invalid-backup")


@pytest.mark.parametrize(
    ("name", "replacement"),
    [
        (
            "idx_workspaces_org_identity",
            "CREATE UNIQUE INDEX idx_workspaces_org_identity "
            "ON workspaces((id),organization_id)",
        ),
        (
            "idx_workspace_members_default",
            "CREATE UNIQUE INDEX idx_workspace_members_default "
            "ON workspace_members(user_id) WHERE status='active'",
        ),
        (
            "idx_workspace_invitation_pending",
            "CREATE UNIQUE INDEX idx_workspace_invitation_pending "
            "ON workspace_invitations(invitee_user_id) WHERE status='pending'",
        ),
        (
            "idx_public_entries_source_article",
            "CREATE UNIQUE INDEX idx_public_entries_source_article "
            "ON public_entries(author_id,source_article_id) WHERE source_article_id IS NOT NULL",
        ),
    ],
)
def test_backup_rejects_incorrect_named_correctness_indexes(
    tmp_path: Path, name: str, replacement: str,
):
    source = tmp_path / "source"
    source.mkdir()
    platform, _user, _article, _raw = seed_instance(source)
    with platform.connect() as db:
        db.execute(f'DROP INDEX "{name}"')
        db.execute(replacement)
    assert_backup_contract_rejects(source, tmp_path, "invalid correctness index")


def test_backup_rejects_incorrect_legacy_workspace_identity_index(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    seed_minimum_legacy_instance(source)
    with sqlite3.connect(source / ".platform" / "platform.sqlite3") as db:
        db.execute("ALTER TABLE workspaces ADD COLUMN organization_id TEXT")
        db.execute("CREATE INDEX idx_workspaces_org_identity ON workspaces(display_name)")
    assert_backup_contract_rejects(source, tmp_path, "invalid correctness index")


@pytest.mark.parametrize(
    ("name", "replacement"),
    [
        (
            "idx_workspace_members_default",
            "CREATE UNIQUE INDEX idx_workspace_members_default "
            "ON workspace_members(user_id) WHERE is_default=1 AND status='ACTIVE'",
        ),
        (
            "idx_workspace_invitation_pending",
            "CREATE UNIQUE INDEX idx_workspace_invitation_pending "
            "ON workspace_invitations(workspace_id,invitee_user_id) WHERE status='PENDING'",
        ),
        (
            "idx_workspace_members_default",
            "CREATE UNIQUE INDEX idx_workspace_members_default ON workspace_members(user_id) "
            "WHERE is_default=(1 AND status)='active'",
        ),
        (
            "idx_public_entries_source_article",
            "CREATE UNIQUE INDEX idx_public_entries_source_article "
            "ON public_entries(author_id,source_workspace_id,source_article_id) "
            "WHERE (source_workspace_id IS NOT NULL AND source_article_id) IS NOT NULL",
        ),
    ],
)
def test_backup_rejects_correctness_index_token_bypasses(
    tmp_path: Path, name: str, replacement: str,
):
    source = tmp_path / "source"
    source.mkdir()
    platform, _user, _article, _raw = seed_instance(source)
    with platform.connect() as db:
        db.execute(f'DROP INDEX "{name}"')
        db.execute(replacement)
    assert_backup_contract_rejects(source, tmp_path, "invalid correctness index")


def test_missing_correctness_indexes_are_migrated_when_data_is_valid(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    platform, _user, _article, _raw = seed_instance(source)
    with platform.connect() as db:
        for name in backup_restore.PLATFORM_CORRECTNESS_INDEXES:
            db.execute(f'DROP INDEX "{name}"')

    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    verify_backup(backup)
    target = tmp_path / "target"
    restore_backup(backup, target)
    restored = PlatformStore(target)
    with restored.connect() as db:
        for name in backup_restore.PLATFORM_CORRECTNESS_INDEXES:
            assert_correctness_index(db, name)
        db.execute("PRAGMA foreign_keys=ON")
        workspace = db.execute(
            "SELECT organization_id,owner_id FROM workspaces LIMIT 1"
        ).fetchone()
        second_workspace = "4" * 32
        db.execute(
            "INSERT INTO workspaces "
            "(id,owner_id,organization_id,root_name,display_name,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'active','2026-01-01','2026-01-01')",
            (second_workspace, workspace[1], workspace[0], second_workspace, "Second"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO workspace_members "
                "(organization_id,workspace_id,user_id,role,status,is_default,added_by,created_at,updated_at) "
                "VALUES(?,?,?,'owner','active',1,?,'2026-01-01','2026-01-01')",
                (workspace[0], second_workspace, workspace[1], workspace[1]),
            )
        db.rollback()

        invitee_id = "5" * 32
        db.execute(
            "INSERT INTO users(id,email,nickname,password_hash,role,status,created_at) "
            "VALUES(?,'restored-invitee@example.com','Invitee','unused','user','active','2026-01-01')",
            (invitee_id,),
        )
        invitation_values = (
            workspace[0], _user["workspace_id"], invitee_id, workspace[1],
            "2027-01-01", "2026-01-01", "2026-01-01",
        )
        db.execute(
            "INSERT INTO workspace_invitations "
            "(id,organization_id,workspace_id,invitee_user_id,role,status,invited_by,expires_at,created_at,updated_at) "
            "VALUES(?,?,?,?,'viewer','pending',?,?,?,?)",
            ("6" * 32, *invitation_values),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO workspace_invitations "
                "(id,organization_id,workspace_id,invitee_user_id,role,status,invited_by,expires_at,created_at,updated_at) "
                "VALUES(?,?,?,?,'viewer','pending',?,?,?,?)",
                ("7" * 32, *invitation_values),
            )
        db.rollback()

        public_values = (
            workspace[1], "2026-01-01", "2026-01-01", _user["workspace_id"], "8" * 32,
        )
        db.execute(
            "INSERT INTO public_entries "
            "(id,author_id,status,current_revision_id,created_at,updated_at,source_workspace_id,source_article_id) "
            "VALUES(? ,?,'published',NULL,?,?,?,?)",
            ("9" * 32, *public_values),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO public_entries "
                "(id,author_id,status,current_revision_id,created_at,updated_at,source_workspace_id,source_article_id) "
                "VALUES(? ,?,'published',NULL,?,?,?,?)",
                ("a" * 32, *public_values),
            )
        db.rollback()


@pytest.mark.parametrize(
    "conflict",
    ["workspace_default", "pending_invitation", "public_source_article"],
)
def test_backup_rejects_data_that_prevents_required_index_migration(
    tmp_path: Path, conflict: str,
):
    source = tmp_path / "source"
    source.mkdir()
    platform, user, _article, _raw = seed_instance(source)
    timestamp = "2026-01-01T00:00:00Z"
    with platform.connect() as db:
        workspace = db.execute(
            "SELECT organization_id FROM workspaces WHERE id=?", (user["workspace_id"],),
        ).fetchone()
        organization_id = workspace[0]
        if conflict == "workspace_default":
            db.execute("DROP INDEX idx_workspace_members_default")
            second_workspace = "c" * 32
            db.execute(
                "INSERT INTO workspaces "
                "(id,owner_id,organization_id,root_name,display_name,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'active',?,?)",
                (
                    second_workspace, user["id"], organization_id, second_workspace,
                    "Second", timestamp, timestamp,
                ),
            )
            db.execute(
                "INSERT INTO workspace_members "
                "(organization_id,workspace_id,user_id,role,status,is_default,added_by,created_at,updated_at) "
                "VALUES(?,?,?,'owner','active',1,?,?,?)",
                (organization_id, second_workspace, user["id"], user["id"], timestamp, timestamp),
            )
            workspace_root = source / "spaces" / second_workspace
            (workspace_root / "wiki").mkdir(parents=True)
            (workspace_root / "raw").mkdir()
        elif conflict == "pending_invitation":
            db.execute("DROP INDEX idx_workspace_invitation_pending")
            invitee_id = "d" * 32
            db.execute(
                "INSERT INTO users(id,email,nickname,password_hash,role,status,created_at) "
                "VALUES(?,?,?,'unused','user','active',?)",
                (invitee_id, "invitee@example.com", "Invitee", timestamp),
            )
            for invitation_id in ("e" * 32, "f" * 32):
                db.execute(
                    "INSERT INTO workspace_invitations "
                    "(id,organization_id,workspace_id,invitee_user_id,role,status,invited_by,expires_at,created_at,updated_at) "
                    "VALUES(?,?,?,?,'viewer','pending',?,?,?,?)",
                    (
                        invitation_id, organization_id, user["workspace_id"], invitee_id,
                        user["id"], "2027-01-01T00:00:00Z", timestamp, timestamp,
                    ),
                )
        else:
            db.execute("DROP INDEX idx_public_entries_source_article")
            for entry_id in ("1" * 32, "2" * 32):
                db.execute(
                    "INSERT INTO public_entries "
                    "(id,author_id,status,current_revision_id,created_at,updated_at,source_workspace_id,source_article_id) "
                    "VALUES(?,?,'published',NULL,?,?,?,?)",
                    (
                        entry_id, user["id"], timestamp, timestamp,
                        user["workspace_id"], "3" * 32,
                    ),
                )
    assert_backup_contract_rejects(source, tmp_path, "cannot create required correctness indexes")


def test_backup_restores_migratable_legacy_platform_idempotency_schema(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    platform, user, _article, _raw = seed_instance(source)
    with platform.connect() as db:
        db.execute("DROP TABLE platform_idempotency")
        db.execute("""
            CREATE TABLE platform_idempotency (
                user_id TEXT NOT NULL, endpoint TEXT NOT NULL, key TEXT NOT NULL,
                payload_hash TEXT NOT NULL, status TEXT NOT NULL, response_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(user_id,endpoint,key)
            )
        """)
        db.execute(
            "INSERT INTO platform_idempotency VALUES(?,?,?,?,?,?,?,?)",
            (user["id"], "/api/test", "key", "hash", "done", '{}', "2026-01-01", "2026-01-01"),
        )
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    target = tmp_path / "target"
    restore_backup(backup, target)
    restored = PlatformStore(target)
    with restored.connect() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(platform_idempotency)")}
        row = db.execute("SELECT scope,status FROM platform_idempotency").fetchone()
    assert "scope" in columns and "user_id" not in columns
    assert tuple(row) == (f"account:{user['id']}", "done")


@pytest.mark.parametrize(
    "blocker",
    [
        "trigger_with_sessions", "trigger_without_sessions", "foreign_key_restrict",
        "foreign_key_cascade", "foreign_key_mixed_case_cascade",
    ],
)
def test_session_revocation_blockers_are_rejected_before_restore(tmp_path: Path, blocker: str):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    platform_database = source / ".platform" / "platform.sqlite3"
    install_session_delete_blocker(platform_database, blocker)
    with pytest.raises(RuntimeError, match="unsupported triggers|session references|revoke restored sessions"):
        create_backup(source, tmp_path / "invalid-source-backup")

    clean = tmp_path / "clean"
    clean.mkdir()
    seed_instance(clean)
    backup = tmp_path / "backup-001"
    create_backup(clean, backup)
    install_session_delete_blocker(backup / ".platform" / "platform.sqlite3", blocker)
    write_manifest(backup)
    with pytest.raises(RuntimeError, match="unsupported triggers|session references|revoke restored sessions"):
        verify_backup(backup)
    target = tmp_path / "target"
    with pytest.raises(RuntimeError, match="unsupported triggers|session references|revoke restored sessions"):
        restore_backup(backup, target)
    assert not target.exists()


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
    assert verify_backup(backup)["schema_version"] == 2


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

    with pytest.raises(RuntimeError, match="must not contain SQLite sidecars"):
        verify_backup(backup)


@pytest.mark.parametrize("invalid_kind", ["orphan_wal", "root_extra"])
def test_restore_rejects_every_backup_rejected_by_verify(tmp_path: Path, invalid_kind: str):
    source = tmp_path / "source"
    source.mkdir()
    _platform, user, _article, _raw = seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if invalid_kind == "orphan_wal":
        state_root = backup / "spaces" / user["workspace_root_name"] / ".wiki-state"
        database = state_root / "state.sqlite3"
        database.unlink()
        (state_root / "state.sqlite3-wal").write_bytes(b"orphan wal")
        relative = database.relative_to(backup).as_posix()
        manifest["files"] = [item for item in manifest["files"] if item["path"] != relative]
    else:
        extra = backup / "extra.txt"
        extra.write_bytes(b"extra")
        manifest["files"].append({
            "path": "extra.txt", "size": extra.stat().st_size, "sha256": backup_restore._sha256(extra),
        })
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError):
        verify_backup(backup)
    target = tmp_path / "target"
    with pytest.raises(RuntimeError):
        restore_backup(backup, target)
    assert not (target / ".platform").exists()
    assert not (target / "spaces").exists()
    assert not (target / ".restore").exists()


def test_restore_rejects_backup_manifest_change_during_verification(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    real_verify = backup_restore.verify_backup
    changed = False

    def verify_then_change(path: Path):
        nonlocal changed
        result = real_verify(path)
        if Path(path).resolve() == backup.resolve() and not changed:
            manifest_path = backup / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["created_at"] = "changed-after-verification"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            changed = True
        return result

    monkeypatch.setattr(backup_restore, "verify_backup", verify_then_change)
    target = tmp_path / "target"
    with pytest.raises(RuntimeError, match="changed during verification"):
        restore_backup(backup, target)
    assert not target.exists()


@pytest.mark.parametrize("database_kind", ["platform", "workspace"])
def test_application_schema_is_required_for_create_verify_and_restore(
    tmp_path: Path, database_kind: str,
):
    def database(root: Path, root_name: str) -> Path:
        if database_kind == "platform":
            return root / ".platform" / "platform.sqlite3"
        return root / "spaces" / root_name / ".wiki-state" / "state.sqlite3"

    source = tmp_path / "source"
    source.mkdir()
    _platform, user, _article, _raw = seed_instance(source)
    invalid = database(source, user["workspace_root_name"])
    invalid.unlink()
    with sqlite3.connect(invalid) as db:
        db.execute("CREATE TABLE unrelated(value TEXT)")
    with pytest.raises(RuntimeError, match="SQLite schema is unsupported"):
        create_backup(source, tmp_path / "invalid-source-backup")

    clean = tmp_path / "clean"
    clean.mkdir()
    _platform, clean_user, _article, _raw = seed_instance(clean)
    backup = tmp_path / "backup-001"
    create_backup(clean, backup)
    invalid = database(backup, clean_user["workspace_root_name"])
    invalid.unlink()
    with sqlite3.connect(invalid) as db:
        db.execute("CREATE TABLE unrelated(value TEXT)")
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    relative = invalid.relative_to(backup).as_posix()
    entry = next(item for item in manifest["files"] if item["path"] == relative)
    entry["size"] = invalid.stat().st_size
    entry["sha256"] = backup_restore._sha256(invalid)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="SQLite schema is unsupported"):
        verify_backup(backup)
    target = tmp_path / "target"
    with pytest.raises(RuntimeError, match="SQLite schema is unsupported"):
        restore_backup(backup, target)
    assert not (target / ".platform").exists()
    assert not (target / "spaces").exists()


@pytest.mark.parametrize("removed_directory", ["workspace", "empty_category"])
def test_manifest_authenticates_workspace_and_empty_directories(
    tmp_path: Path, removed_directory: str,
):
    source = tmp_path / "source"
    source.mkdir()
    _platform, user, _article, _raw = seed_instance(source)
    workspace_relative = Path("spaces") / user["workspace_root_name"]
    empty_category_relative = workspace_relative / "wiki" / "empty-category"
    (source / empty_category_relative).mkdir()
    backup = tmp_path / "backup-001"
    manifest = create_backup(source, backup)
    assert workspace_relative.as_posix() in manifest["directories"]
    assert empty_category_relative.as_posix() in manifest["directories"]

    if removed_directory == "workspace":
        backup_restore.shutil.rmtree(backup / workspace_relative)
    else:
        (backup / empty_category_relative).rmdir()
    with pytest.raises(RuntimeError, match="file set|directory set|workspace layout"):
        verify_backup(backup)
    target = tmp_path / "target"
    with pytest.raises(RuntimeError, match="file set|directory set|workspace layout"):
        restore_backup(backup, target)
    assert not (target / ".platform").exists()
    assert not (target / "spaces").exists()


def test_restore_preserves_empty_category_directories(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _platform, user, _article, _raw = seed_instance(source)
    relative = Path("spaces") / user["workspace_root_name"] / "wiki" / "empty-category"
    (source / relative).mkdir()
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    target = tmp_path / "target"
    restore_backup(backup, target)
    assert (target / relative).is_dir()


def test_deleted_team_workspace_remains_required_and_round_trips(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    platform, user, article, _raw = seed_instance(source)
    with platform.connect() as db:
        organization_id = db.execute(
            "SELECT organization_id FROM workspaces WHERE id=?", (user["workspace_id"],),
        ).fetchone()[0]
        db.execute(
            "UPDATE organizations SET kind='team',personal_owner_id=NULL WHERE id=?",
            (organization_id,),
        )
        db.execute("UPDATE workspaces SET status='deleted' WHERE id=?", (user["workspace_id"],))
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    target = tmp_path / "target"
    restore_backup(backup, target)
    restored_article = target / article.relative_to(source)
    assert restored_article.read_bytes() == article.read_bytes()

    missing_source = tmp_path / "missing-source"
    backup_restore.shutil.copytree(source, missing_source)
    backup_restore.shutil.rmtree(missing_source / "spaces" / user["workspace_root_name"])
    with pytest.raises(RuntimeError, match="workspace layout is incomplete"):
        create_backup(missing_source, tmp_path / "missing-backup")


def test_deleted_personal_workspace_tombstone_may_have_no_data_root(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    platform, user, _article, _raw = seed_instance(source)
    mark_deleted_personal_tombstone(platform, user)
    backup_restore.shutil.rmtree(source / "spaces" / user["workspace_root_name"])
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    assert verify_backup(backup)["schema_version"] == 2
    target = tmp_path / "target"
    restore_backup(backup, target)
    assert not (target / "spaces" / user["workspace_root_name"]).exists()


@pytest.mark.parametrize("residual_kind", ["deleted_personal", "orphan"])
def test_backup_rejects_retired_or_orphan_workspace_roots(tmp_path: Path, residual_kind: str):
    source = tmp_path / "source"
    source.mkdir()
    platform, user, _article, _raw = seed_instance(source)
    if residual_kind == "deleted_personal":
        mark_deleted_personal_tombstone(platform, user)
        residual_name = user["workspace_root_name"]
    else:
        residual_name = "f" * 32
    residual = source / "spaces" / residual_name
    (residual / "wiki").mkdir(parents=True, exist_ok=True)
    (residual / "raw").mkdir(exist_ok=True)
    secret = residual / "wiki" / "secret.md"
    secret.write_text("# Deleted private content\n", encoding="utf-8")
    destination = tmp_path / "invalid-source-backup"
    with pytest.raises(RuntimeError, match="retired or orphan workspace roots"):
        create_backup(source, destination)
    assert not destination.exists()

    backup_restore.shutil.rmtree(residual)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    forged = backup / "spaces" / residual_name
    (forged / "wiki").mkdir(parents=True)
    (forged / "raw").mkdir()
    (forged / "wiki" / "secret.md").write_text("# Deleted private content\n", encoding="utf-8")
    write_manifest(backup)
    assert any(item["path"].endswith("secret.md") for item in json.loads(
        (backup / "manifest.json").read_text(encoding="utf-8")
    )["files"])
    with pytest.raises(RuntimeError, match="retired or orphan workspace roots"):
        verify_backup(backup)
    target = tmp_path / "target"
    with pytest.raises(RuntimeError, match="retired or orphan workspace roots"):
        restore_backup(backup, target)
    assert not target.exists()


def test_restore_rejects_backup_root_symlink_that_verify_rejects(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    seed_instance(source)
    backup = tmp_path / "backup-001"
    create_backup(source, backup)
    link = tmp_path / "backup-link"
    link.symlink_to(backup, target_is_directory=True)
    with pytest.raises(RuntimeError, match="backup root must not be a symbolic link"):
        verify_backup(link)
    target = tmp_path / "target"
    with pytest.raises(RuntimeError, match="backup root must not be a symbolic link"):
        restore_backup(link, target)
    assert not target.exists()


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

    source_database = database(source)
    backup_restore._check_sqlite(source_database, checkpoint=True)
    source_database.write_bytes(b"truncated database")
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
