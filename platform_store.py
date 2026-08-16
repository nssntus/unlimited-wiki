"""Platform identity, tenant metadata, encrypted secrets, and public review state."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from publication import FINGERPRINT_VERSION, article_id_from_markdown, normalize_text, snapshot_fingerprint


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
ROLES = {"user", "admin"}
ORGANIZATION_ROLES = {"owner", "admin", "member"}
WORKSPACE_ROLES = {"owner", "editor", "viewer"}
WORKSPACE_PERMISSIONS = {
    "viewer": frozenset({"wiki.read"}),
    "editor": frozenset({"wiki.read", "wiki.write", "wiki.govern"}),
    "owner": frozenset({"wiki.read", "wiki.write", "wiki.govern", "workspace.manage", "model.manage"}),
}
SUBMISSION_STATES = {
    "ai_queued", "ai_reviewing", "ai_failed", "needs_revision", "ai_rejected",
    "pending_admin", "admin_changes_requested", "admin_rejected", "approved", "withdrawn",
}
PUBLIC_STATES = {"published", "withdrawn_by_author", "removed_by_admin", "superseded"}
ACTIVE_SUBMISSION_STATES = {"ai_queued", "ai_reviewing", "pending_admin"}


class PlatformIdempotencyError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def future_iso(*, hours: int = 0, minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours, minutes=minutes)).isoformat(timespec="seconds")


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    if len(password) < 10 or len(password) > 1024:
        raise ValueError("password must be between 10 and 1024 characters")
    salt = os.urandom(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(derived).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.urlsafe_b64decode(salt),
            n=int(n), r=int(r), p=int(p), dklen=32,
        )
        return hmac.compare_digest(derived, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class AccountSessionContext:
    user_id: str
    email: str
    nickname: str
    role: str
    csrf_token: str
    expires_at: str
    current_workspace_id: str | None

    def public(self) -> dict:
        return {
            "user": {"id": self.user_id, "email": self.email, "nickname": self.nickname, "role": self.role},
            "workspace": None,
            "workspace_selection_required": True,
            "csrf_token": self.csrf_token,
            "session_expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class SessionContext:
    user_id: str
    email: str
    nickname: str
    role: str
    workspace_id: str
    workspace_root_name: str
    workspace_name: str
    workspace_kind: str
    organization_id: str
    organization_role: str
    workspace_role: str
    csrf_token: str
    expires_at: str

    def public(self) -> dict:
        permissions = sorted(WORKSPACE_PERMISSIONS.get(self.workspace_role, frozenset()))
        return {
            "user": {"id": self.user_id, "email": self.email, "nickname": self.nickname, "role": self.role},
            "workspace": {
                "id": self.workspace_id,
                "organization_id": self.organization_id,
                "kind": self.workspace_kind,
                "display_name": self.workspace_name,
                "status": "active",
                "role": self.workspace_role,
                "organization_role": self.organization_role,
                "permissions": permissions,
            },
            "csrf_token": self.csrf_token,
            "session_expires_at": self.expires_at,
            "workspace_selection_required": False,
        }


class SecretVault:
    def __init__(self, state_root: Path):
        self.path = state_root / "master.key"
        state_root.mkdir(parents=True, exist_ok=True)
        os.chmod(state_root, 0o700)
        if self.path.exists():
            key = self.path.read_bytes()
            if len(key) != 32:
                raise RuntimeError("platform master key is invalid")
        else:
            key = AESGCM.generate_key(bit_length=256)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
        os.chmod(self.path, 0o600)
        self._key = key
        self._cipher = AESGCM(key)

    def encrypt(self, plaintext: str, *, scope: str) -> str:
        if not plaintext:
            return ""
        nonce = os.urandom(12)
        encrypted = self._cipher.encrypt(nonce, plaintext.encode("utf-8"), scope.encode("utf-8"))
        return base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")

    def decrypt(self, value: str, *, scope: str) -> str:
        if not value:
            return ""
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        return self._cipher.decrypt(raw[:12], raw[12:], scope.encode("utf-8")).decode("utf-8")

    def derive(self, value: str, *, scope: str) -> str:
        digest = hmac.new(self._key, f"{scope}\0{value}".encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class _BorrowedConnection:
    """Let nested store methods join an outer platform transaction."""

    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def execute(self, sql: str, parameters=()):
        if sql.lstrip().upper().startswith("BEGIN"):
            return self
        return self._db.execute(sql, parameters)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def __getattr__(self, name: str):
        return getattr(self._db, name)


class PlatformStore:
    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        self.state_root = self.project_root / ".platform"
        self.spaces_root = self.project_root / "spaces"
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.spaces_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_root, 0o700)
        os.chmod(self.spaces_root, 0o700)
        self.db_path = self.state_root / "platform.sqlite3"
        self.vault = SecretVault(self.state_root)
        self._lock = threading.RLock()
        self._transaction_context = threading.local()
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        borrowed = getattr(self._transaction_context, "connection", None)
        if borrowed is not None:
            return _BorrowedConnection(borrowed)
        return self._new_connection()

    def _new_connection(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, nickname TEXT NOT NULL,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','admin')),
                    status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS organizations (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('personal','team')),
                    personal_owner_id TEXT UNIQUE REFERENCES users(id), display_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','deleted')),
                    created_by TEXT REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
                    organization_id TEXT REFERENCES organizations(id), root_name TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','suspended','deleted')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS organization_members (
                    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('owner','admin','member')),
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended')),
                    added_by TEXT REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(organization_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS workspace_members (
                    organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('owner','editor','viewer')),
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended')),
                    is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)),
                    added_by TEXT REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, user_id),
                    FOREIGN KEY(organization_id, user_id) REFERENCES organization_members(organization_id, user_id),
                    FOREIGN KEY(workspace_id, organization_id) REFERENCES workspaces(id, organization_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_members_default
                    ON workspace_members(user_id) WHERE is_default=1 AND status='active';
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    csrf_hash TEXT NOT NULL, current_workspace_id TEXT REFERENCES workspaces(id),
                    expires_at TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspace_invitations (
                    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    invitee_user_id TEXT NOT NULL REFERENCES users(id), role TEXT NOT NULL
                        CHECK(role IN ('editor','viewer')),
                    status TEXT NOT NULL CHECK(status IN ('pending','accepted','declined','revoked','expired')),
                    invited_by TEXT NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_invitation_pending
                    ON workspace_invitations(workspace_id,invitee_user_id) WHERE status='pending';
                CREATE INDEX IF NOT EXISTS idx_workspace_invitation_invitee
                    ON workspace_invitations(invitee_user_id,status,created_at DESC);
                CREATE TABLE IF NOT EXISTS platform_idempotency (
                    scope TEXT NOT NULL, endpoint TEXT NOT NULL, key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('running','done')),
                    response_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(scope,endpoint,key)
                );
                CREATE TABLE IF NOT EXISTS recovery_codes (
                    code_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL, used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS login_attempts (
                    scope_hash TEXT PRIMARY KEY, failures INTEGER NOT NULL, blocked_until TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_settings (
                    workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL, base_url_enc TEXT NOT NULL, api_key_enc TEXT NOT NULL,
                    model TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS share_previews (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    article_path TEXT NOT NULL, source_revision TEXT NOT NULL, content_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
                    article_id TEXT, publication_fingerprint TEXT
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    status TEXT NOT NULL, snapshot_json TEXT NOT NULL, content_hash TEXT NOT NULL,
                    ai_report_json TEXT, reason TEXT, reviewer_id TEXT REFERENCES users(id), public_entry_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, article_id TEXT, article_path TEXT,
                    source_revision TEXT, publication_fingerprint TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_submissions_owner ON submissions(owner_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status, created_at);
                CREATE TABLE IF NOT EXISTS public_entries (
                    id TEXT PRIMARY KEY, author_id TEXT NOT NULL REFERENCES users(id), status TEXT NOT NULL,
                    current_revision_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    moderation_reason TEXT, moderated_by TEXT REFERENCES users(id), moderated_at TEXT,
                    source_workspace_id TEXT, source_article_id TEXT
                );
                CREATE TABLE IF NOT EXISTS public_revisions (
                    id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES public_entries(id), submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id),
                    version INTEGER NOT NULL, snapshot_json TEXT NOT NULL, content_hash TEXT NOT NULL,
                    published_at TEXT NOT NULL, publication_fingerprint TEXT, UNIQUE(entry_id, version)
                );
                CREATE TABLE IF NOT EXISTS reports (
                    id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES public_entries(id), reporter_id TEXT REFERENCES users(id),
                    reason_code TEXT NOT NULL, detail TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                    resolution TEXT, resolved_by TEXT REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT NOT NULL,
                    title TEXT NOT NULL, message TEXT NOT NULL, read_at TEXT, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY, actor_id TEXT, action TEXT NOT NULL, object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS migrations (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL UNIQUE, workspace_id TEXT NOT NULL,
                    status TEXT NOT NULL, manifest_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)
            workspace_columns = {row["name"] for row in db.execute("PRAGMA table_info(workspaces)").fetchall()}
            if "organization_id" not in workspace_columns:
                db.execute("ALTER TABLE workspaces ADD COLUMN organization_id TEXT REFERENCES organizations(id)")
            if "status" not in workspace_columns:
                db.execute("ALTER TABLE workspaces ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            if "updated_at" not in workspace_columns:
                db.execute("ALTER TABLE workspaces ADD COLUMN updated_at TEXT")
                db.execute("UPDATE workspaces SET updated_at=created_at WHERE updated_at IS NULL")
            session_columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)").fetchall()}
            if "current_workspace_id" not in session_columns:
                db.execute("ALTER TABLE sessions ADD COLUMN current_workspace_id TEXT REFERENCES workspaces(id)")
                db.execute("""
                    UPDATE sessions SET current_workspace_id=(
                        SELECT workspace_id FROM workspace_members member
                        WHERE member.user_id=sessions.user_id AND member.status='active'
                        ORDER BY member.is_default DESC,member.created_at,member.workspace_id LIMIT 1
                    )
                """)
            idempotency_columns = {row["name"] for row in db.execute("PRAGMA table_info(platform_idempotency)").fetchall()}
            if "scope" not in idempotency_columns:
                db.execute("ALTER TABLE platform_idempotency RENAME TO platform_idempotency_legacy")
                db.execute("""
                    CREATE TABLE platform_idempotency (
                        scope TEXT NOT NULL, endpoint TEXT NOT NULL, key TEXT NOT NULL,
                        payload_hash TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('running','done')),
                        response_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        PRIMARY KEY(scope,endpoint,key)
                    )
                """)
                db.execute("""
                    INSERT INTO platform_idempotency
                    SELECT 'account:' || user_id,endpoint,key,payload_hash,status,response_json,created_at,updated_at
                    FROM platform_idempotency_legacy WHERE status='done'
                """)
                db.execute("DROP TABLE platform_idempotency_legacy")
            db.commit()
            self._remove_workspace_owner_unique(db)
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_org_identity ON workspaces(id, organization_id)")
            db.execute("BEGIN IMMEDIATE")
            self._backfill_memberships(db)
            public_columns = {row["name"] for row in db.execute("PRAGMA table_info(public_entries)").fetchall()}
            for name, declaration in {
                "moderation_reason": "TEXT",
                "moderated_by": "TEXT REFERENCES users(id)",
                "moderated_at": "TEXT",
                "source_workspace_id": "TEXT",
                "source_article_id": "TEXT",
            }.items():
                if name not in public_columns:
                    db.execute(f"ALTER TABLE public_entries ADD COLUMN {name} {declaration}")
            for table, columns in {
                "share_previews": {
                    "article_id": "TEXT",
                    "publication_fingerprint": "TEXT",
                },
                "submissions": {
                    "article_id": "TEXT",
                    "article_path": "TEXT",
                    "source_revision": "TEXT",
                    "publication_fingerprint": "TEXT",
                },
                "public_revisions": {
                    "publication_fingerprint": "TEXT",
                },
            }.items():
                existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
                for name, declaration in columns.items():
                    if name not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            self._backfill_publication_metadata(db)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_submissions_article
                ON submissions(owner_id,workspace_id,article_id,updated_at DESC)
            """)
            db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_public_entries_source_article
                ON public_entries(author_id,source_workspace_id,source_article_id)
                WHERE source_workspace_id IS NOT NULL AND source_article_id IS NOT NULL
            """)
            db.execute("UPDATE submissions SET status='ai_queued',updated_at=? WHERE status='ai_reviewing'", (now_iso(),))
        os.chmod(self.db_path, 0o600)

    @staticmethod
    def _backfill_publication_metadata(db: sqlite3.Connection) -> None:
        for table in ("share_previews", "submissions"):
            rows = db.execute(
                f"SELECT id,snapshot_json,article_id,publication_fingerprint FROM {table}"
            ).fetchall()
            for row in rows:
                if row["article_id"] and re.fullmatch(
                    rf"{re.escape(FINGERPRINT_VERSION)}:[a-f0-9]{{64}}",
                    str(row["publication_fingerprint"] or ""),
                ):
                    continue
                try:
                    snapshot = json.loads(row["snapshot_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                article_id = row["article_id"] or article_id_from_markdown(str(snapshot.get("markdown", "")))
                fingerprint = snapshot_fingerprint(snapshot)
                db.execute(
                    f"UPDATE {table} SET article_id=?,publication_fingerprint=? WHERE id=?",
                    (article_id, fingerprint, row["id"]),
                )

        revisions = db.execute(
            "SELECT id,snapshot_json,publication_fingerprint FROM public_revisions"
        ).fetchall()
        for row in revisions:
            if re.fullmatch(
                rf"{re.escape(FINGERPRINT_VERSION)}:[a-f0-9]{{64}}",
                str(row["publication_fingerprint"] or ""),
            ):
                continue
            try:
                fingerprint = snapshot_fingerprint(json.loads(row["snapshot_json"]))
            except (TypeError, json.JSONDecodeError):
                continue
            db.execute(
                "UPDATE public_revisions SET publication_fingerprint=? WHERE id=?",
                (fingerprint, row["id"]),
            )

        candidates = db.execute("""
            SELECT entry.id,entry.author_id,submission.workspace_id,submission.article_id
            FROM public_entries entry
            JOIN public_revisions revision ON revision.id=entry.current_revision_id
            JOIN submissions submission ON submission.id=revision.submission_id
            WHERE submission.article_id IS NOT NULL
        """).fetchall()
        grouped: dict[tuple[str, str, str], list[str]] = {}
        for row in candidates:
            key = (row["author_id"], row["workspace_id"], row["article_id"])
            grouped.setdefault(key, []).append(row["id"])
        for (author_id, workspace_id, article_id), entry_ids in grouped.items():
            if len(entry_ids) != 1:
                continue
            db.execute("""
                UPDATE public_entries SET source_workspace_id=?,source_article_id=?
                WHERE id=? AND author_id=? AND source_workspace_id IS NULL AND source_article_id IS NULL
            """, (workspace_id, article_id, entry_ids[0], author_id))

    @staticmethod
    def _remove_workspace_owner_unique(db: sqlite3.Connection) -> None:
        owner_unique = False
        for index in db.execute("PRAGMA index_list(workspaces)").fetchall():
            if not index["unique"]:
                continue
            columns = [row["name"] for row in db.execute(f"PRAGMA index_info('{index['name']}')").fetchall()]
            if columns == ["owner_id"]:
                owner_unique = True
                break
        if not owner_unique:
            return

        db.execute("PRAGMA foreign_keys=OFF")
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE workspaces_rebuild (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
                    organization_id TEXT REFERENCES organizations(id), root_name TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','suspended','deleted')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )
            """)
            db.execute("""
                INSERT INTO workspaces_rebuild(
                    id,owner_id,organization_id,root_name,display_name,status,created_at,updated_at
                )
                SELECT id,owner_id,organization_id,root_name,display_name,status,created_at,updated_at
                FROM workspaces
            """)
            db.execute("DROP TABLE workspaces")
            db.execute("ALTER TABLE workspaces_rebuild RENAME TO workspaces")
            db.execute("CREATE UNIQUE INDEX idx_workspaces_org_identity ON workspaces(id, organization_id)")
            violations = db.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("workspace schema migration failed foreign key validation")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys=ON")
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("workspace schema migration did not restore foreign key enforcement")

    @staticmethod
    def _personal_organization_id(user_id: str) -> str:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"unlimited-wiki:personal-organization:{user_id}").hex

    def _backfill_memberships(self, db: sqlite3.Connection) -> None:
        rows = db.execute("""
            SELECT u.id user_id,u.nickname,w.id workspace_id,w.organization_id,w.created_at
            FROM users u JOIN workspaces w ON w.owner_id=u.id
            WHERE w.organization_id IS NULL
        """).fetchall()
        for row in rows:
            organization_id = row["organization_id"] or self._personal_organization_id(row["user_id"])
            created = row["created_at"] or now_iso()
            db.execute("""
                INSERT INTO organizations
                    (id,kind,personal_owner_id,display_name,status,created_by,created_at,updated_at)
                VALUES(?, 'personal', ?, ?, 'active', ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
            """, (organization_id, row["user_id"], f"{row['nickname']} 的个人组织", row["user_id"], created, created))
            db.execute("UPDATE workspaces SET organization_id=? WHERE id=?", (organization_id, row["workspace_id"]))
            db.execute("""
                INSERT INTO organization_members
                    (organization_id,user_id,role,status,added_by,created_at,updated_at)
                VALUES(?,?,'owner','active',?,?,?)
                ON CONFLICT(organization_id,user_id) DO NOTHING
            """, (organization_id, row["user_id"], row["user_id"], created, created))
            db.execute("""
                INSERT INTO workspace_members
                    (organization_id,workspace_id,user_id,role,status,is_default,added_by,created_at,updated_at)
                VALUES(?,?,?,'owner','active',?,?,?,?)
                ON CONFLICT(workspace_id,user_id) DO NOTHING
            """, (
                organization_id,
                row["workspace_id"],
                row["user_id"],
                0 if db.execute(
                    "SELECT 1 FROM workspace_members WHERE user_id=? AND status='active' AND is_default=1",
                    (row["user_id"],),
                ).fetchone() else 1,
                row["user_id"],
                created,
                created,
            ))

    def audit(self, actor_id: str | None, action: str, object_type: str, object_id: str, detail: dict | None = None) -> None:
        with self.connect() as db:
            self._audit(db, actor_id, action, object_type, object_id, detail)

    @staticmethod
    def _audit(
        db: sqlite3.Connection, actor_id: str | None, action: str, object_type: str,
        object_id: str, detail: dict | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, actor_id, action, object_type, object_id,
             json.dumps(detail or {}, ensure_ascii=False), now_iso()),
        )

    @staticmethod
    def _notify(db: sqlite3.Connection, user_id: str, kind: str, object_type: str, object_id: str, title: str, message: str) -> None:
        db.execute(
            "INSERT INTO notifications VALUES(?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, user_id, kind, object_type, object_id, title, message, None, now_iso()),
        )

    def list_notifications(self, context: SessionContext) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 200",
                (context.user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def read_notification(self, context: SessionContext, notification_id: str) -> dict:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE notifications SET read_at=COALESCE(read_at,?) WHERE id=? AND user_id=?",
                (now_iso(), notification_id, context.user_id),
            ).rowcount
            row = db.execute(
                "SELECT * FROM notifications WHERE id=? AND user_id=?",
                (notification_id, context.user_id),
            ).fetchone()
        if not changed or row is None:
            raise FileNotFoundError(notification_id)
        return dict(row)

    def register(self, email: str, nickname: str, password: str) -> tuple[dict, str]:
        email = email.strip().casefold()
        nickname = nickname.strip()
        if not EMAIL_RE.fullmatch(email) or len(email) > 254:
            raise ValueError("invalid email address")
        if not nickname or len(nickname) > 80:
            raise ValueError("nickname must be between 1 and 80 characters")
        password_hash = hash_password(password)
        user_id, workspace_id = uuid.uuid4().hex, uuid.uuid4().hex
        organization_id = self._personal_organization_id(user_id)
        root_name = workspace_id
        recovery = secrets.token_urlsafe(24)
        created = now_iso()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            role = "admin" if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0 else "user"
            try:
                db.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)", (user_id, email, nickname, password_hash, role, "active", created))
                db.execute("""
                    INSERT INTO organizations
                        (id,kind,personal_owner_id,display_name,status,created_by,created_at,updated_at)
                    VALUES(?,'personal',?,?,'active',?,?,?)
                """, (organization_id, user_id, f"{nickname} 的个人组织", user_id, created, created))
                db.execute("""
                    INSERT INTO organization_members
                        (organization_id,user_id,role,status,added_by,created_at,updated_at)
                    VALUES(?,?,'owner','active',?,?,?)
                """, (organization_id, user_id, user_id, created, created))
                db.execute("""
                    INSERT INTO workspaces(
                        id,owner_id,organization_id,root_name,display_name,status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,'active',?,?)
                """, (workspace_id, user_id, organization_id, root_name, f"{nickname} 的 Wiki", created, created))
                db.execute("""
                    INSERT INTO workspace_members
                        (organization_id,workspace_id,user_id,role,status,is_default,added_by,created_at,updated_at)
                    VALUES(?,?,?,'owner','active',1,?,?,?)
                """, (organization_id, workspace_id, user_id, user_id, created, created))
                db.execute("INSERT INTO recovery_codes VALUES(?,?,?,NULL)", (hash_token(recovery), user_id, future_iso(hours=24)))
                db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise ValueError("registration could not be completed") from exc
        root = self.workspace_root(root_name)
        (root / "wiki").mkdir(parents=True, exist_ok=True)
        (root / "raw").mkdir(parents=True, exist_ok=True)
        self.audit(user_id, "user.register", "user", user_id, {"role": role})
        return {"id": user_id, "workspace_id": workspace_id, "workspace_root_name": root_name, "role": role}, recovery

    def authenticate(self, email: str, password: str, *, remote: str = "local") -> dict | None:
        email = email.strip().casefold()
        scope = hash_token(f"{remote}\0{email}")
        with self.connect() as db:
            attempt = db.execute("SELECT * FROM login_attempts WHERE scope_hash=?", (scope,)).fetchone()
        if attempt and attempt["blocked_until"] and attempt["blocked_until"] > now_iso():
            raise RuntimeError("too many login attempts; try again later")
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE email=? AND status='active'", (email,)).fetchone()
        if row is None:
            # Keep the expensive path for unknown accounts to reduce timing disclosure.
            hash_password(password if 10 <= len(password) <= 1024 else "invalid-password")
            valid = False
        else:
            valid = verify_password(password, row["password_hash"])
        with self._lock, self.connect() as db:
            if valid:
                db.execute("DELETE FROM login_attempts WHERE scope_hash=?", (scope,))
            else:
                prior = db.execute("SELECT failures FROM login_attempts WHERE scope_hash=?", (scope,)).fetchone()
                failures = (prior[0] if prior else 0) + 1
                blocked = future_iso(minutes=15) if failures >= 5 else None
                db.execute("""
                    INSERT INTO login_attempts VALUES(?,?,?,?)
                    ON CONFLICT(scope_hash) DO UPDATE SET failures=excluded.failures,blocked_until=excluded.blocked_until,updated_at=excluded.updated_at
                """, (scope, failures, blocked, now_iso()))
        return dict(row) if valid else None

    def create_session(self, user_id: str, *, hours: int = 12) -> tuple[str, SessionContext]:
        token = secrets.token_urlsafe(32)
        csrf = self.vault.derive(token, scope="session-csrf")
        expires = future_iso(hours=hours)
        with self.connect() as db:
            workspace = db.execute("""
                SELECT member.workspace_id FROM workspace_members member
                JOIN workspaces workspace ON workspace.id=member.workspace_id AND workspace.status='active'
                JOIN organizations organization
                  ON organization.id=workspace.organization_id AND organization.status='active'
                JOIN organization_members organization_member
                  ON organization_member.organization_id=workspace.organization_id
                 AND organization_member.user_id=member.user_id AND organization_member.status='active'
                WHERE member.user_id=? AND member.status='active'
                ORDER BY member.is_default DESC,member.created_at,member.workspace_id LIMIT 1
            """, (user_id,)).fetchone()
            if workspace is None:
                raise RuntimeError("user has no active workspace")
            db.execute(
                """INSERT INTO sessions(
                    token_hash,user_id,csrf_hash,current_workspace_id,expires_at,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (hash_token(token), user_id, hash_token(csrf), workspace["workspace_id"], expires, now_iso()),
            )
        context = self.resolve_session(token)
        if context is None:
            raise RuntimeError("session creation failed")
        self.audit(user_id, "session.create", "user", user_id)
        return token, context

    def resolve_session(self, token: str) -> SessionContext | None:
        if not token:
            return None
        with self.connect() as db:
            row = db.execute("""
                SELECT u.id user_id,u.email,u.nickname,u.role,u.status,
                       w.id workspace_id,w.root_name,w.display_name,w.organization_id,
                       om.role organization_role,wm.role workspace_role,o.kind workspace_kind,
                       o.status organization_status,w.status workspace_status,
                       s.csrf_hash,s.expires_at
                FROM sessions s
                JOIN users u ON u.id=s.user_id
                JOIN workspace_members wm ON wm.user_id=u.id AND wm.status='active'
                    AND wm.workspace_id=s.current_workspace_id
                JOIN workspaces w ON w.id=wm.workspace_id AND w.organization_id=wm.organization_id
                JOIN organization_members om ON om.organization_id=w.organization_id
                    AND om.user_id=u.id AND om.status='active'
                JOIN organizations o ON o.id=w.organization_id
                WHERE s.token_hash=? AND s.expires_at>?
            """, (hash_token(token), now_iso())).fetchone()
        if (
            row is None
            or row["status"] != "active"
            or row["organization_status"] != "active"
            or row["workspace_status"] != "active"
        ):
            return None
        csrf = self.vault.derive(token, scope="session-csrf")
        if not hmac.compare_digest(hash_token(csrf), row["csrf_hash"]):
            return None
        return SessionContext(
            row["user_id"], row["email"], row["nickname"], row["role"], row["workspace_id"],
            row["root_name"], row["display_name"], row["workspace_kind"], row["organization_id"], row["organization_role"],
            row["workspace_role"], csrf, row["expires_at"],
        )

    def resolve_account_session(self, token: str) -> AccountSessionContext | None:
        if not token:
            return None
        with self.connect() as db:
            row = db.execute("""
                SELECT u.id user_id,u.email,u.nickname,u.role,u.status,
                       s.csrf_hash,s.expires_at,s.current_workspace_id
                FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.expires_at>?
            """, (hash_token(token), now_iso())).fetchone()
        if row is None or row["status"] != "active":
            return None
        csrf = self.vault.derive(token, scope="session-csrf")
        if not hmac.compare_digest(hash_token(csrf), row["csrf_hash"]):
            return None
        return AccountSessionContext(
            row["user_id"], row["email"], row["nickname"], row["role"], csrf,
            row["expires_at"], row["current_workspace_id"],
        )

    def authorize_workspace(self, user_id: str, workspace_id: str, permission: str) -> dict:
        with self.connect() as db:
            row = self._workspace_access_row(db, user_id, workspace_id)
        self._check_workspace_permission(row, workspace_id, permission)
        return dict(row)

    def _authorize_workspace_in_transaction(
        self, db: sqlite3.Connection, user_id: str, workspace_id: str, permission: str,
    ) -> sqlite3.Row:
        row = self._workspace_access_row(db, user_id, workspace_id)
        self._check_workspace_permission(row, workspace_id, permission)
        return row

    @staticmethod
    def _authorize_admin_in_transaction(db: sqlite3.Connection, user_id: str) -> None:
        row = db.execute("SELECT role,status FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None or row["status"] != "active" or row["role"] != "admin":
            raise PermissionError("admin role required")

    @contextlib.contextmanager
    def authorized_workspace_action(self, user_id: str, workspace_id: str, permission: str):
        """Serialize a final side effect with membership changes in this process."""
        with self._lock:
            with self.connect() as db:
                row = self._workspace_access_row(db, user_id, workspace_id)
            self._check_workspace_permission(row, workspace_id, permission)
            yield

    @staticmethod
    def _workspace_access_row(db: sqlite3.Connection, user_id: str, workspace_id: str) -> sqlite3.Row | None:
        return db.execute("""
            SELECT w.*,wm.role workspace_role,wm.status workspace_member_status,
                   om.role organization_role,om.status organization_member_status,
                   o.kind organization_kind,o.status organization_status,u.status user_status
            FROM workspaces w
            JOIN workspace_members wm ON wm.workspace_id=w.id AND wm.organization_id=w.organization_id
            JOIN organization_members om ON om.organization_id=w.organization_id AND om.user_id=wm.user_id
            JOIN organizations o ON o.id=w.organization_id
            JOIN users u ON u.id=wm.user_id
            WHERE w.id=? AND wm.user_id=?
        """, (workspace_id, user_id)).fetchone()

    @staticmethod
    def _check_workspace_permission(row: sqlite3.Row | None, workspace_id: str, permission: str) -> None:
        if (
            row is None
            or row["user_status"] != "active"
            or row["organization_status"] != "active"
            or row["status"] != "active"
            or row["workspace_member_status"] != "active"
            or row["organization_member_status"] != "active"
        ):
            raise FileNotFoundError(workspace_id)
        allowed = WORKSPACE_PERMISSIONS.get(row["workspace_role"], frozenset())
        if permission not in allowed:
            raise PermissionError(permission)

    @staticmethod
    def _workspace_summary(row: sqlite3.Row | dict, *, current: bool = False) -> dict:
        role = row["workspace_role"]
        status = row["status"]
        kind = row["organization_kind"]
        return {
            "id": row["id"],
            "organization_id": row["organization_id"],
            "kind": kind,
            "display_name": row["display_name"],
            "status": status,
            "role": role,
            "organization_role": row["organization_role"],
            "permissions": sorted(WORKSPACE_PERMISSIONS.get(role, frozenset())),
            "current": current,
            "can_suspend": kind == "team" and role == "owner" and status == "active",
            "can_restore": kind == "team" and role == "owner" and status == "suspended",
            "can_delete": kind == "team" and role == "owner" and status == "suspended",
            "can_leave": kind == "team" and role != "owner" and status in {"active", "suspended"},
        }

    def list_workspaces(
        self, context: SessionContext | AccountSessionContext, *, include_inactive: bool = False,
    ) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT workspace.*,organization.kind organization_kind,
                       workspace_member.role workspace_role,
                       organization_member.role organization_role
                FROM workspace_members workspace_member
                JOIN workspaces workspace ON workspace.id=workspace_member.workspace_id
                JOIN organizations organization ON organization.id=workspace.organization_id
                JOIN organization_members organization_member
                  ON organization_member.organization_id=workspace.organization_id
                 AND organization_member.user_id=workspace_member.user_id
                JOIN users user ON user.id=workspace_member.user_id
                WHERE workspace_member.user_id=? AND workspace_member.status='active'
                  AND organization_member.status='active' AND user.status='active'
                  AND organization.status='active'
                  AND (
                    workspace.status='active'
                    OR (?=1 AND (workspace.status='suspended' OR workspace_member.role='owner'))
                  )
                ORDER BY CASE organization.kind WHEN 'personal' THEN 0 ELSE 1 END,
                         workspace.created_at,workspace.id
            """, (context.user_id, 1 if include_inactive else 0)).fetchall()
        current_workspace_id = getattr(context, "workspace_id", None) or context.current_workspace_id
        return [self._workspace_summary(row, current=row["id"] == current_workspace_id) for row in rows]

    def workspace_summary_for_user(self, user_id: str, workspace_id: str) -> dict:
        with self._lock, self.connect() as db:
            row = self._workspace_access_row(db, user_id, workspace_id)
        if (
            row is None
            or row["user_status"] != "active"
            or row["organization_status"] != "active"
            or row["workspace_member_status"] != "active"
            or row["organization_member_status"] != "active"
        ):
            raise FileNotFoundError(workspace_id)
        return self._workspace_summary(row, current=False)

    @staticmethod
    def _validate_workspace_name(value: str) -> str:
        value = value.strip()
        if not value or len(value) > 80 or any(ord(ch) < 32 for ch in value):
            raise ValueError("workspace name must be between 1 and 80 characters")
        return value

    def create_team(self, context: SessionContext | AccountSessionContext, display_name: str) -> dict:
        display_name = self._validate_workspace_name(display_name)
        organization_id, workspace_id = uuid.uuid4().hex, uuid.uuid4().hex
        root = self.workspace_root(workspace_id)
        created = now_iso()
        created_root = False
        try:
            root.mkdir()
            created_root = True
            self._register_transaction_rollback(lambda: shutil.rmtree(root) if root.exists() else None)
            (root / "wiki").mkdir()
            (root / "raw").mkdir()
            with self._lock, self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                user = db.execute(
                    "SELECT status FROM users WHERE id=?", (context.user_id,),
                ).fetchone()
                if user is None or user["status"] != "active":
                    raise FileNotFoundError(context.user_id)
                db.execute("""
                    INSERT INTO organizations(
                        id,kind,personal_owner_id,display_name,status,created_by,created_at,updated_at
                    ) VALUES(?,'team',NULL,?,'active',?,?,?)
                """, (organization_id, display_name, context.user_id, created, created))
                db.execute("""
                    INSERT INTO organization_members(
                        organization_id,user_id,role,status,added_by,created_at,updated_at
                    ) VALUES(?,?,'owner','active',?,?,?)
                """, (organization_id, context.user_id, context.user_id, created, created))
                db.execute("""
                    INSERT INTO workspaces(
                        id,owner_id,organization_id,root_name,display_name,status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,'active',?,?)
                """, (workspace_id, context.user_id, organization_id, workspace_id, display_name, created, created))
                db.execute("""
                    INSERT INTO workspace_members(
                        organization_id,workspace_id,user_id,role,status,is_default,added_by,created_at,updated_at
                    ) VALUES(?,?,?,'owner','active',0,?,?,?)
                """, (organization_id, workspace_id, context.user_id, context.user_id, created, created))
                db.execute(
                    "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, context.user_id, "workspace.create", "workspace", workspace_id,
                     json.dumps({"kind": "team", "display_name": display_name}, ensure_ascii=False), created),
                )
                db.commit()
        except BaseException:
            if created_root and root.exists():
                shutil.rmtree(root)
            raise
        with self.connect() as db:
            row = self._workspace_access_row(db, context.user_id, workspace_id)
        return self._workspace_summary(row)

    def list_workspace_members(self, context: SessionContext) -> list[dict]:
        with self._lock, self.connect() as db:
            access = self._workspace_access_row(db, context.user_id, context.workspace_id)
            self._check_workspace_permission(access, context.workspace_id, "workspace.manage")
            rows = db.execute("""
                SELECT user.id user_id,user.email,user.nickname,member.role,member.status,
                       organization_member.role organization_role,member.created_at
                FROM workspace_members member
                JOIN users user ON user.id=member.user_id
                JOIN organization_members organization_member
                  ON organization_member.organization_id=member.organization_id
                 AND organization_member.user_id=member.user_id
                WHERE member.workspace_id=? AND member.status='active'
                  AND organization_member.status='active' AND user.status='active'
                ORDER BY CASE member.role WHEN 'owner' THEN 0 WHEN 'editor' THEN 1 ELSE 2 END,
                         member.created_at,user.id
            """, (context.workspace_id,)).fetchall()
        return [{**dict(row), "is_current_user": row["user_id"] == context.user_id} for row in rows]

    def invite_workspace_member(self, context: SessionContext, email: str, role: str) -> dict:
        email = email.strip().casefold()
        if role not in {"editor", "viewer"}:
            raise ValueError("invitation role must be editor or viewer")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            access = self._workspace_access_row(db, context.user_id, context.workspace_id)
            self._check_workspace_permission(access, context.workspace_id, "workspace.manage")
            if access["organization_kind"] != "team":
                raise ValueError("personal workspaces cannot invite members")
            invitee = db.execute(
                "SELECT id,status FROM users WHERE email=?", (email,),
            ).fetchone()
            if invitee is None or invitee["status"] != "active":
                raise ValueError("the invited account is unavailable")
            if invitee["id"] == context.user_id:
                raise ValueError("cannot invite yourself")
            active = db.execute("""
                SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=? AND status='active'
            """, (context.workspace_id, invitee["id"])).fetchone()
            if active:
                raise ValueError("user is already a workspace member")
            invitation_id, created = uuid.uuid4().hex, now_iso()
            db.execute("""
                UPDATE workspace_invitations SET status='expired',updated_at=?
                WHERE workspace_id=? AND invitee_user_id=? AND status='pending' AND expires_at<=?
            """, (created, context.workspace_id, invitee["id"], created))
            try:
                db.execute("""
                    INSERT INTO workspace_invitations(
                        id,organization_id,workspace_id,invitee_user_id,role,status,invited_by,
                        expires_at,created_at,updated_at
                    ) VALUES(?,?,?,?,?,'pending',?,?,?,?)
                """, (
                    invitation_id, access["organization_id"], context.workspace_id, invitee["id"], role,
                    context.user_id, future_iso(hours=24 * 7), created, created,
                ))
            except sqlite3.IntegrityError as exc:
                raise ValueError("a pending invitation already exists") from exc
            self._notify(
                db, invitee["id"], "workspace_invitation", "workspace", context.workspace_id,
                "新的团队空间邀请", f"你被邀请加入 {access['display_name']}。",
            )
            db.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, context.user_id, "workspace.invite", "workspace", context.workspace_id,
                 json.dumps({"invitation_id": invitation_id, "role": role}, ensure_ascii=False), created),
            )
            db.commit()
        return {"id": invitation_id, "workspace_id": context.workspace_id, "role": role, "status": "pending"}

    def list_invitations(self, context: SessionContext | AccountSessionContext) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT invitation.id,invitation.workspace_id,invitation.role,invitation.status,
                       invitation.expires_at,invitation.created_at,workspace.display_name,
                       inviter.nickname invited_by_nickname
                FROM workspace_invitations invitation
                JOIN workspaces workspace ON workspace.id=invitation.workspace_id
                JOIN organizations organization ON organization.id=workspace.organization_id
                JOIN users inviter ON inviter.id=invitation.invited_by
                WHERE invitation.invitee_user_id=? AND invitation.status='pending'
                  AND invitation.expires_at>? AND workspace.status='active'
                  AND organization.status='active'
                ORDER BY invitation.created_at DESC
            """, (context.user_id, now_iso())).fetchall()
        return [dict(row) for row in rows]

    def respond_to_invitation(self, context: SessionContext | AccountSessionContext, invitation_id: str, *, accept: bool) -> dict:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            invitation = db.execute("""
                SELECT invitation.*,workspace.display_name,workspace.status workspace_status,
                       organization.status organization_status,user.status user_status
                FROM workspace_invitations invitation
                JOIN workspaces workspace ON workspace.id=invitation.workspace_id
                JOIN organizations organization ON organization.id=invitation.organization_id
                JOIN users user ON user.id=invitation.invitee_user_id
                WHERE invitation.id=? AND invitation.invitee_user_id=?
            """, (invitation_id, context.user_id)).fetchone()
            if invitation is None:
                raise FileNotFoundError(invitation_id)
            if invitation["status"] == "pending" and invitation["expires_at"] <= now_iso():
                db.execute(
                    "UPDATE workspace_invitations SET status='expired',updated_at=? WHERE id=?",
                    (now_iso(), invitation_id),
                )
                db.commit()
                return {"id": invitation_id, "workspace_id": invitation["workspace_id"], "status": "expired"}
            if (
                invitation["status"] != "pending"
                or invitation["workspace_status"] != "active"
                or invitation["organization_status"] != "active"
                or invitation["user_status"] != "active"
            ):
                raise ValueError("invitation is no longer available")
            created = now_iso()
            status = "accepted" if accept else "declined"
            if accept:
                db.execute("""
                    INSERT INTO organization_members(
                        organization_id,user_id,role,status,added_by,created_at,updated_at
                    ) VALUES(?,?,'member','active',?,?,?)
                    ON CONFLICT(organization_id,user_id) DO UPDATE SET
                        status='active',updated_at=excluded.updated_at
                """, (
                    invitation["organization_id"], context.user_id, invitation["invited_by"], created, created,
                ))
                db.execute("""
                    INSERT INTO workspace_members(
                        organization_id,workspace_id,user_id,role,status,is_default,added_by,created_at,updated_at
                    ) VALUES(?,?,?,?,'active',0,?,?,?)
                    ON CONFLICT(workspace_id,user_id) DO UPDATE SET
                        role=excluded.role,status='active',updated_at=excluded.updated_at
                """, (
                    invitation["organization_id"], invitation["workspace_id"], context.user_id,
                    invitation["role"], invitation["invited_by"], created, created,
                ))
            db.execute(
                "UPDATE workspace_invitations SET status=?,updated_at=? WHERE id=? AND status='pending'",
                (status, created, invitation_id),
            )
            self._notify(
                db, invitation["invited_by"], "workspace_invitation_response", "workspace",
                invitation["workspace_id"], "团队空间邀请已处理",
                "受邀用户已接受邀请。" if accept else "受邀用户已拒绝邀请。",
            )
            db.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, context.user_id, f"workspace.invitation_{status}", "workspace",
                 invitation["workspace_id"], json.dumps({"invitation_id": invitation_id}), created),
            )
            db.commit()
        return {"id": invitation_id, "workspace_id": invitation["workspace_id"], "status": status}

    def change_workspace_member_role(self, context: SessionContext, user_id: str, role: str) -> dict:
        if role not in {"editor", "viewer"}:
            raise ValueError("member role must be editor or viewer")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            access = self._workspace_access_row(db, context.user_id, context.workspace_id)
            self._check_workspace_permission(access, context.workspace_id, "workspace.manage")
            target = db.execute("""
                SELECT member.*,user.email,user.nickname,user.status user_status
                FROM workspace_members member JOIN users user ON user.id=member.user_id
                WHERE member.workspace_id=? AND member.user_id=? AND member.status='active'
            """, (context.workspace_id, user_id)).fetchone()
            if target is None or target["user_status"] != "active":
                raise FileNotFoundError(user_id)
            if target["role"] == "owner":
                raise ValueError("transfer ownership before changing the owner role")
            old_role, changed = target["role"], now_iso()
            db.execute(
                "UPDATE workspace_members SET role=?,updated_at=? WHERE workspace_id=? AND user_id=?",
                (role, changed, context.workspace_id, user_id),
            )
            db.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, context.user_id, "workspace.member_role", "workspace", context.workspace_id,
                 json.dumps({"user_id": user_id, "old_role": old_role, "new_role": role}), changed),
            )
            db.commit()
        return {"user_id": user_id, "email": target["email"], "nickname": target["nickname"], "role": role, "status": "active"}

    def remove_workspace_member(self, context: SessionContext, user_id: str) -> dict:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            access = self._workspace_access_row(db, context.user_id, context.workspace_id)
            self._check_workspace_permission(access, context.workspace_id, "workspace.manage")
            target = db.execute("""
                SELECT * FROM workspace_members
                WHERE workspace_id=? AND user_id=? AND status='active'
            """, (context.workspace_id, user_id)).fetchone()
            if target is None:
                raise FileNotFoundError(user_id)
            if target["role"] == "owner":
                raise ValueError("transfer ownership before removing the owner")
            changed = now_iso()
            db.execute("""
                UPDATE workspace_members SET status='suspended',is_default=0,updated_at=?
                WHERE workspace_id=? AND user_id=?
            """, (changed, context.workspace_id, user_id))
            remaining = db.execute("""
                SELECT 1 FROM workspace_members
                WHERE organization_id=? AND user_id=? AND status='active' LIMIT 1
            """, (target["organization_id"], user_id)).fetchone()
            if remaining is None:
                db.execute("""
                    UPDATE organization_members SET status='suspended',updated_at=?
                    WHERE organization_id=? AND user_id=?
                """, (changed, target["organization_id"], user_id))
            db.execute(
                "DELETE FROM sessions WHERE user_id=? AND current_workspace_id=?",
                (user_id, context.workspace_id),
            )
            db.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, context.user_id, "workspace.member_remove", "workspace", context.workspace_id,
                 json.dumps({"user_id": user_id, "old_role": target["role"]}), changed),
            )
            db.commit()
        return {"user_id": user_id, "status": "suspended"}

    def transfer_workspace_owner(self, context: SessionContext, user_id: str) -> dict:
        if user_id == context.user_id:
            raise ValueError("target user is already the owner")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            access = self._workspace_access_row(db, context.user_id, context.workspace_id)
            self._check_workspace_permission(access, context.workspace_id, "workspace.manage")
            if access["organization_kind"] != "team":
                raise ValueError("personal workspace ownership cannot be transferred")
            if access["workspace_role"] != "owner":
                raise PermissionError("workspace.manage")
            target = self._workspace_access_row(db, user_id, context.workspace_id)
            self._check_workspace_permission(target, context.workspace_id, "wiki.read")
            changed = now_iso()
            db.execute("""
                UPDATE workspace_members SET role='editor',updated_at=?
                WHERE workspace_id=? AND user_id=?
            """, (changed, context.workspace_id, context.user_id))
            db.execute("""
                UPDATE workspace_members SET role='owner',updated_at=?
                WHERE workspace_id=? AND user_id=?
            """, (changed, context.workspace_id, user_id))
            db.execute("UPDATE workspaces SET owner_id=?,updated_at=? WHERE id=?", (
                user_id, changed, context.workspace_id,
            ))
            db.execute("""
                UPDATE organization_members SET role='owner',updated_at=?
                WHERE organization_id=? AND user_id=?
            """, (changed, access["organization_id"], user_id))
            other_owned = db.execute("""
                SELECT 1 FROM workspace_members
                WHERE organization_id=? AND user_id=? AND role='owner' AND status='active' LIMIT 1
            """, (access["organization_id"], context.user_id)).fetchone()
            if other_owned is None:
                db.execute("""
                    UPDATE organization_members SET role='member',updated_at=?
                    WHERE organization_id=? AND user_id=?
                """, (changed, access["organization_id"], context.user_id))
            db.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, context.user_id, "workspace.owner_transfer", "workspace", context.workspace_id,
                 json.dumps({"old_owner_id": context.user_id, "new_owner_id": user_id, "reason": "manual_transfer"}), changed),
            )
            self._notify(
                db, user_id, "workspace_owner_transfer", "workspace", context.workspace_id,
                "你已成为团队空间 Owner", f"你现在负责管理 {access['display_name']}。",
            )
            db.commit()
        return {"workspace_id": context.workspace_id, "old_owner_id": context.user_id, "new_owner_id": user_id}

    def switch_workspace(self, token: str, context: SessionContext | AccountSessionContext, workspace_id: str) -> SessionContext:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            target = self._workspace_access_row(db, context.user_id, workspace_id)
            self._check_workspace_permission(target, workspace_id, "wiki.read")
            if db.execute("""
                UPDATE sessions SET current_workspace_id=?
                WHERE token_hash=? AND user_id=? AND expires_at>?
            """, (workspace_id, hash_token(token), context.user_id, now_iso())).rowcount != 1:
                raise FileNotFoundError("session")
            db.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, context.user_id, "session.workspace_switch", "workspace", workspace_id,
                 json.dumps({"from_workspace_id": getattr(context, "workspace_id", None) or context.current_workspace_id}), now_iso()),
            )
            db.commit()
        selected = self.resolve_session(token)
        if selected is None:
            raise RuntimeError("workspace switch failed")
        return selected

    def rename_workspace(self, context: SessionContext, display_name: str) -> dict:
        display_name = self._validate_workspace_name(display_name)
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            access = self._workspace_access_row(db, context.user_id, context.workspace_id)
            self._check_workspace_permission(access, context.workspace_id, "workspace.manage")
            changed = now_iso()
            db.execute(
                "UPDATE workspaces SET display_name=?,updated_at=? WHERE id=?",
                (display_name, changed, context.workspace_id),
            )
            if access["organization_kind"] == "team":
                db.execute(
                    "UPDATE organizations SET display_name=?,updated_at=? WHERE id=?",
                    (display_name, changed, access["organization_id"]),
                )
            db.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, context.user_id, "workspace.rename", "workspace", context.workspace_id,
                 json.dumps({"old_name": access["display_name"], "new_name": display_name}, ensure_ascii=False), changed),
            )
            db.commit()
        return {"id": context.workspace_id, "display_name": display_name}

    def change_workspace_lifecycle(
        self,
        context: SessionContext | AccountSessionContext,
        workspace_id: str,
        action: str,
    ) -> dict:
        transitions = {
            "suspend": ("active", "suspended"),
            "restore": ("suspended", "active"),
            "delete": ("suspended", "deleted"),
        }
        if action not in transitions:
            raise ValueError("unsupported workspace lifecycle action")
        expected, target = transitions[action]
        stamp = now_iso()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._workspace_access_row(db, context.user_id, workspace_id)
            if (
                row is None
                or row["user_status"] != "active"
                or row["organization_status"] != "active"
                or row["workspace_member_status"] != "active"
                or row["organization_member_status"] != "active"
            ):
                raise FileNotFoundError(workspace_id)
            if row["organization_kind"] != "team":
                raise ValueError("personal workspaces do not support lifecycle changes")
            if row["workspace_role"] != "owner":
                raise PermissionError("workspace.manage")
            if row["status"] != expected:
                raise ValueError(f"workspace must be {expected} before {action}")
            db.execute(
                "UPDATE workspaces SET status=?,updated_at=? WHERE id=? AND status=?",
                (target, stamp, workspace_id, expected),
            )
            affected_sessions = 0
            if target != "active":
                affected_sessions = db.execute(
                    "UPDATE sessions SET current_workspace_id=NULL WHERE current_workspace_id=?",
                    (workspace_id,),
                ).rowcount
                db.execute(
                    "UPDATE workspace_invitations SET status='revoked',updated_at=? WHERE workspace_id=? AND status='pending'",
                    (stamp, workspace_id),
                )
            detail = {"from": expected, "to": target, "affected_session_count": affected_sessions}
            db.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, context.user_id, f"workspace.{action}", "workspace", workspace_id,
                 json.dumps(detail, ensure_ascii=False), stamp),
            )
            title = {"suspend": "团队空间已停用", "restore": "团队空间已恢复", "delete": "团队空间已删除"}[action]
            members = db.execute("""
                SELECT user_id FROM workspace_members
                WHERE workspace_id=? AND status='active' AND user_id<>?
            """, (workspace_id, context.user_id)).fetchall()
            for member in members:
                self._notify(
                    db, member["user_id"], f"workspace_{action}", "workspace", workspace_id,
                    title, row["display_name"],
                )
            db.commit()
            updated = dict(row)
            updated["status"] = target
        return self._workspace_summary(updated, current=False)

    def workspace_storage_state(self, workspace_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("""
                SELECT workspace.id,workspace.root_name,workspace.status
                FROM workspaces workspace
                JOIN organizations organization ON organization.id=workspace.organization_id
                WHERE workspace.id=? AND organization.kind='team'
            """, (workspace_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(workspace_id)
        return dict(row)

    def workspace_storage_states(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT workspace.id,workspace.root_name,workspace.status
                FROM workspaces workspace
                JOIN organizations organization ON organization.id=workspace.organization_id
                WHERE organization.kind='team'
                ORDER BY workspace.created_at,workspace.id
            """).fetchall()
        return [dict(row) for row in rows]

    def leave_workspace(
        self, context: SessionContext | AccountSessionContext, workspace_id: str,
    ) -> dict:
        stamp = now_iso()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._workspace_access_row(db, context.user_id, workspace_id)
            if (
                row is None
                or row["user_status"] != "active"
                or row["organization_status"] != "active"
                or row["workspace_member_status"] != "active"
                or row["organization_member_status"] != "active"
                or row["status"] not in {"active", "suspended"}
            ):
                raise FileNotFoundError(workspace_id)
            if row["organization_kind"] != "team":
                raise ValueError("personal workspaces cannot be left")
            if row["workspace_role"] == "owner":
                raise ValueError("transfer workspace ownership before leaving")
            db.execute("""
                UPDATE workspace_members SET status='suspended',is_default=0,updated_at=?
                WHERE workspace_id=? AND user_id=? AND status='active'
            """, (stamp, workspace_id, context.user_id))
            remaining = db.execute("""
                SELECT 1 FROM workspace_members
                WHERE organization_id=? AND user_id=? AND status='active' LIMIT 1
            """, (row["organization_id"], context.user_id)).fetchone()
            if remaining is None:
                db.execute("""
                    UPDATE organization_members SET status='suspended',updated_at=?
                    WHERE organization_id=? AND user_id=?
                """, (stamp, row["organization_id"], context.user_id))
            affected_sessions = db.execute(
                "UPDATE sessions SET current_workspace_id=NULL WHERE user_id=? AND current_workspace_id=?",
                (context.user_id, workspace_id),
            ).rowcount
            db.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, context.user_id, "workspace.member_leave", "workspace", workspace_id,
                 json.dumps({"role": row["workspace_role"], "affected_session_count": affected_sessions}), stamp),
            )
            owners = db.execute("""
                SELECT user_id FROM workspace_members
                WHERE workspace_id=? AND role='owner' AND status='active'
            """, (workspace_id,)).fetchall()
            for owner in owners:
                self._notify(
                    db, owner["user_id"], "workspace_member_left", "workspace", workspace_id,
                    "成员已退出团队空间", row["display_name"],
                )
            db.commit()
        return {"workspace_id": workspace_id, "status": "left"}

    @staticmethod
    def _platform_payload_hash(data: dict) -> str:
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def run_platform_idempotent(
        self, scope: str, endpoint: str, key: str, data: dict, action,
    ) -> tuple[dict, bool]:
        """Commit the platform mutation and its replay record in one transaction."""
        payload_hash = self._platform_payload_hash(data)
        with self._lock:
            db = self._new_connection()
            self._transaction_context.rollback_callbacks = []
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM platform_idempotency WHERE scope=? AND endpoint=? AND key=?",
                    (scope, endpoint, key),
                ).fetchone()
                if row is not None:
                    if row["payload_hash"] != payload_hash:
                        raise PlatformIdempotencyError("Idempotency-Key was already used with a different request")
                    if row["status"] != "done":
                        raise PlatformIdempotencyError("request with this Idempotency-Key is still in progress")
                    db.commit()
                    return json.loads(row["response_json"]), True
                changed = now_iso()
                db.execute(
                    "INSERT INTO platform_idempotency VALUES(?,?,?,?,'running',NULL,?,?)",
                    (scope, endpoint, key, payload_hash, changed, changed),
                )
                self._transaction_context.connection = db
                response = action()
                db.execute("""
                    UPDATE platform_idempotency SET status='done',response_json=?,updated_at=?
                    WHERE scope=? AND endpoint=? AND key=? AND status='running'
                """, (json.dumps(response, ensure_ascii=False), now_iso(), scope, endpoint, key))
                db.commit()
                return response, False
            except BaseException:
                db.rollback()
                for callback in reversed(self._transaction_context.rollback_callbacks):
                    callback()
                raise
            finally:
                self._transaction_context.connection = None
                self._transaction_context.rollback_callbacks = None
                db.close()

    def _register_transaction_rollback(self, callback) -> None:
        callbacks = getattr(self._transaction_context, "rollback_callbacks", None)
        if callbacks is not None:
            callbacks.append(callback)

    def revoke_session(self, token: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hash_token(token),))

    def revoke_all_sessions(self, user_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        self.audit(user_id, "session.revoke_all", "user", user_id)

    def reset_password(self, email: str, recovery_code: str, new_password: str) -> bool:
        email = email.strip().casefold()
        password_hash = hash_password(new_password)
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT r.code_hash,r.user_id FROM recovery_codes r JOIN users u ON u.id=r.user_id
                WHERE u.email=? AND r.code_hash=? AND r.used_at IS NULL AND r.expires_at>?
            """, (email, hash_token(recovery_code), now_iso())).fetchone()
            if row is None:
                db.rollback()
                return False
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, row["user_id"]))
            db.execute("UPDATE recovery_codes SET used_at=? WHERE code_hash=?", (now_iso(), row["code_hash"]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (row["user_id"],))
            db.commit()
        self.audit(row["user_id"], "user.password_reset", "user", row["user_id"])
        return True

    def set_role(self, user_id: str, role: str, *, actor_id: str | None = None) -> None:
        if role not in ROLES:
            raise ValueError("invalid role")
        with self.connect() as db:
            if db.execute("UPDATE users SET role=? WHERE id=?", (role, user_id)).rowcount != 1:
                raise FileNotFoundError(user_id)
        self.audit(actor_id, "user.role", "user", user_id, {"role": role})

    def account_workspace_ids(self, user_id: str) -> list[str]:
        with self._lock, self.connect() as db:
            rows = db.execute("""
                SELECT DISTINCT workspace.id
                FROM workspaces workspace
                JOIN organizations organization ON organization.id=workspace.organization_id
                LEFT JOIN workspace_members member
                  ON member.workspace_id=workspace.id AND member.user_id=?
                WHERE member.user_id IS NOT NULL
                   OR workspace.owner_id=?
                   OR organization.personal_owner_id=?
                ORDER BY workspace.id
            """, (user_id, user_id, user_id)).fetchall()
        return [row["id"] for row in rows]

    def delete_account(self, context: SessionContext | AccountSessionContext, password: str) -> dict:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            user = db.execute("SELECT * FROM users WHERE id=? AND status='active'", (context.user_id,)).fetchone()
            if user is None or not verify_password(password, user["password_hash"]):
                db.rollback()
                raise ValueError("password is invalid")
            now = now_iso()
            shared_personal = db.execute("""
                SELECT workspace.id FROM workspaces workspace
                JOIN organizations organization
                  ON organization.id=workspace.organization_id
                 AND organization.kind='personal'
                 AND organization.personal_owner_id=?
                WHERE EXISTS (
                    SELECT 1 FROM workspace_members member
                    WHERE member.workspace_id=workspace.id
                      AND member.user_id<>? AND member.status='active'
                )
                LIMIT 1
            """, (context.user_id, context.user_id)).fetchone()
            if shared_personal is not None:
                db.rollback()
                raise ValueError("remove other members from personal workspaces before deleting the account")
            team_workspaces = db.execute("""
                SELECT DISTINCT w.id,w.organization_id,w.owner_id FROM workspaces w
                JOIN organizations o ON o.id=w.organization_id AND o.kind='team'
                LEFT JOIN workspace_members own ON own.workspace_id=w.id AND own.user_id=?
                WHERE (w.owner_id=? OR (own.role='owner' AND own.status='active'))
                  AND w.status IN ('active','suspended') AND o.status<>'deleted'
            """, (context.user_id, context.user_id)).fetchall()
            workspace_transfers = []
            for workspace in team_workspaces:
                candidate = db.execute("""
                    SELECT member.user_id,member.role FROM workspace_members member
                    JOIN organization_members organization_member
                      ON organization_member.organization_id=member.organization_id
                     AND organization_member.user_id=member.user_id
                     AND organization_member.status='active'
                    JOIN users candidate_user ON candidate_user.id=member.user_id AND candidate_user.status='active'
                    WHERE member.workspace_id=? AND member.user_id<>? AND member.status='active'
                    ORDER BY CASE member.role WHEN 'owner' THEN 0 WHEN 'editor' THEN 1 ELSE 2 END,
                             member.created_at,member.user_id
                    LIMIT 1
                """, (workspace["id"], context.user_id)).fetchone()
                if candidate is None:
                    db.rollback()
                    raise ValueError("transfer team workspace ownership before deleting the account")
                workspace_transfers.append((workspace, candidate["user_id"], candidate["role"]))

            team_organizations = db.execute("""
                SELECT organization.id FROM organizations organization
                JOIN organization_members own ON own.organization_id=organization.id
                WHERE organization.kind='team' AND own.user_id=?
                  AND own.role='owner' AND own.status='active'
                  AND organization.status<>'deleted'
                  AND EXISTS (
                    SELECT 1 FROM workspaces live
                    WHERE live.organization_id=organization.id
                      AND live.status IN ('active','suspended')
                  )
            """, (context.user_id,)).fetchall()
            organization_transfers = []
            for organization in team_organizations:
                candidate = db.execute("""
                    SELECT member.user_id,member.role FROM organization_members member
                    JOIN users candidate_user ON candidate_user.id=member.user_id AND candidate_user.status='active'
                    WHERE member.organization_id=? AND member.user_id<>? AND member.status='active'
                    ORDER BY CASE member.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
                             member.created_at,member.user_id
                    LIMIT 1
                """, (organization["id"], context.user_id)).fetchone()
                if candidate is None:
                    db.rollback()
                    raise ValueError("transfer team organization ownership before deleting the account")
                organization_transfers.append((organization["id"], candidate["user_id"], candidate["role"]))

            for organization_id, new_owner_id, previous_role in organization_transfers:
                db.execute("""
                    UPDATE organization_members SET role='owner',updated_at=?
                    WHERE organization_id=? AND user_id=? AND status='active'
                """, (now, organization_id, new_owner_id))
                db.execute(
                    "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, context.user_id, "organization.owner_transfer", "organization",
                     organization_id, json.dumps({
                         "old_owner_id": context.user_id,
                         "new_owner_id": new_owner_id,
                         "previous_role": previous_role,
                         "reason": "account_deletion",
                     }, ensure_ascii=False), now),
                )
            for workspace, new_owner_id, previous_role in workspace_transfers:
                db.execute("""
                    UPDATE workspace_members SET role='owner',updated_at=?
                    WHERE workspace_id=? AND user_id=? AND status='active'
                """, (now, workspace["id"], new_owner_id))
                db.execute("UPDATE workspaces SET owner_id=? WHERE id=?", (new_owner_id, workspace["id"]))
                db.execute(
                    "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, context.user_id, "workspace.owner_transfer", "workspace",
                     workspace["id"], json.dumps({
                         "old_owner_id": context.user_id,
                         "new_owner_id": new_owner_id,
                         "previous_role": previous_role,
                         "reason": "account_deletion",
                     }, ensure_ascii=False), now),
                )

            cleanup = db.execute("""
                SELECT w.id,w.root_name FROM workspaces w
                JOIN organizations o ON o.id=w.organization_id
                WHERE o.kind='personal' AND o.personal_owner_id=?
                  AND NOT EXISTS (
                    SELECT 1 FROM workspace_members other
                    WHERE other.workspace_id=w.id AND other.user_id<>? AND other.status='active'
                  )
            """, (context.user_id, context.user_id)).fetchall()
            db.execute("UPDATE public_entries SET status='withdrawn_by_author',updated_at=? WHERE author_id=? AND status='published'", (now, context.user_id))
            for workspace in cleanup:
                db.execute("DELETE FROM model_settings WHERE workspace_id=?", (workspace["id"],))
            db.execute("DELETE FROM sessions WHERE user_id=?", (context.user_id,))
            db.execute("DELETE FROM recovery_codes WHERE user_id=?", (context.user_id,))
            db.execute("DELETE FROM workspace_members WHERE user_id=?", (context.user_id,))
            db.execute("DELETE FROM organization_members WHERE user_id=?", (context.user_id,))
            db.execute("""
                UPDATE organizations SET status='deleted',updated_at=?
                WHERE kind='personal' AND personal_owner_id=?
                  AND NOT EXISTS (
                    SELECT 1 FROM organization_members member
                    WHERE member.organization_id=organizations.id AND member.status='active'
                  )
            """, (now, context.user_id))
            db.execute("UPDATE users SET email=?,nickname='已注销用户',password_hash=?,status='deleted' WHERE id=?", (
                f"deleted-{context.user_id}@invalid.local", hash_password(secrets.token_urlsafe(32)), context.user_id,
            ))
            db.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, context.user_id, "user.delete", "user", context.user_id,
                 json.dumps({
                     "workspace_transfers": [
                         {"workspace_id": workspace["id"], "new_owner_id": new_owner_id}
                         for workspace, new_owner_id, _previous_role in workspace_transfers
                     ],
                 }, ensure_ascii=False), now),
            )
            db.commit()
        return {
            "cleanup_workspaces": [dict(row) for row in cleanup],
            "workspace_transfers": [
                {"workspace_id": workspace["id"], "new_owner_id": new_owner_id}
                for workspace, new_owner_id, _previous_role in workspace_transfers
            ],
        }

    def workspace_root(self, root_name: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", root_name):
            raise ValueError("invalid workspace root")
        root = (self.spaces_root / root_name).resolve()
        if not root.is_relative_to(self.spaces_root.resolve()):
            raise ValueError("invalid workspace root")
        return root

    def migration(self, kind: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM migrations WHERE kind=?", (kind,)).fetchone()
        return dict(row) if row else None

    def record_migration(self, migration_id: str, kind: str, workspace_id: str, status: str, manifest: dict) -> None:
        now = now_iso()
        with self.connect() as db:
            db.execute("""
                INSERT INTO migrations VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(kind) DO UPDATE SET status=excluded.status,manifest_json=excluded.manifest_json,updated_at=excluded.updated_at
            """, (migration_id, kind, workspace_id, status, json.dumps(manifest, ensure_ascii=False), now, now))

    def user_workspace(self, user_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("""
                SELECT w.* FROM workspace_members wm JOIN workspaces w ON w.id=wm.workspace_id
                WHERE wm.user_id=? AND wm.status='active' AND wm.is_default=1
            """, (user_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(user_id)
        return dict(row)

    def save_model(self, workspace_id: str, provider: str, base_url: str, api_key: str, model: str) -> None:
        scope = f"workspace:{workspace_id}:model"
        with self.connect() as db:
            existing = db.execute("SELECT api_key_enc FROM model_settings WHERE workspace_id=?", (workspace_id,)).fetchone()
            encrypted_key = self.vault.encrypt(api_key, scope=scope) if api_key else (existing[0] if existing else "")
            db.execute("""
                INSERT INTO model_settings VALUES(?,?,?,?,?,?)
                ON CONFLICT(workspace_id) DO UPDATE SET provider=excluded.provider,base_url_enc=excluded.base_url_enc,
                  api_key_enc=excluded.api_key_enc,model=excluded.model,updated_at=excluded.updated_at
            """, (workspace_id, provider, self.vault.encrypt(base_url, scope=scope), encrypted_key, model, now_iso()))

    def load_model(self, workspace_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM model_settings WHERE workspace_id=?", (workspace_id,)).fetchone()
        if row is None:
            return {}
        scope = f"workspace:{workspace_id}:model"
        return {
            "provider": row["provider"], "base_url": self.vault.decrypt(row["base_url_enc"], scope=scope),
            "api_key": self.vault.decrypt(row["api_key_enc"], scope=scope), "model": row["model"],
        }

    def create_preview(
        self,
        context: SessionContext,
        article_path: str,
        source_revision: str,
        article_id: str,
        publication_fingerprint: str,
        snapshot: dict,
    ) -> dict:
        self.authorize_workspace(context.user_id, context.workspace_id, "wiki.write")
        if not re.fullmatch(r"[a-f0-9]{32}", article_id or ""):
            raise ValueError("article has no stable identity")
        if publication_fingerprint != snapshot_fingerprint(snapshot):
            raise ValueError("publication fingerprint does not match snapshot")
        preview_id = uuid.uuid4().hex
        canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        expires = future_iso(minutes=30)
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_workspace_in_transaction(
                db, context.user_id, context.workspace_id, "wiki.write",
            )
            db.execute("""
                INSERT INTO share_previews(
                    id,owner_id,workspace_id,article_path,source_revision,content_hash,snapshot_json,
                    expires_at,created_at,article_id,publication_fingerprint
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, (
                preview_id, context.user_id, context.workspace_id, article_path, source_revision,
                content_hash, canonical, expires, now_iso(), article_id, publication_fingerprint,
            ))
            db.commit()
        return {
            "preview_id": preview_id,
            "expires_at": expires,
            "source_revision": source_revision,
            "content_hash": content_hash,
            "publication_fingerprint": publication_fingerprint,
            "snapshot": snapshot,
        }

    def submit_preview(self, context: SessionContext, preview_id: str) -> dict:
        self.authorize_workspace(context.user_id, context.workspace_id, "wiki.write")
        submission_id = uuid.uuid4().hex
        now = now_iso()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_workspace_in_transaction(
                db, context.user_id, context.workspace_id, "wiki.write",
            )
            row = db.execute("SELECT * FROM share_previews WHERE id=? AND owner_id=? AND workspace_id=? AND expires_at>?", (
                preview_id, context.user_id, context.workspace_id, now,
            )).fetchone()
            if row is None:
                db.rollback()
                raise FileNotFoundError(preview_id)
            db.execute("""
                INSERT INTO submissions(
                    id,owner_id,workspace_id,status,snapshot_json,content_hash,ai_report_json,reason,
                    reviewer_id,public_entry_id,created_at,updated_at,article_id,article_path,
                    source_revision,publication_fingerprint
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                submission_id, context.user_id, context.workspace_id, "ai_queued", row["snapshot_json"],
                row["content_hash"], None, None, None, None, now, now, row["article_id"],
                row["article_path"], row["source_revision"], row["publication_fingerprint"],
            ))
            db.execute("DELETE FROM share_previews WHERE id=?", (preview_id,))
            saved = db.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
            self._audit(
                db, context.user_id, "submission.create", "submission", submission_id,
                {"content_hash": row["content_hash"]},
            )
            db.commit()
        return self._submission_public(saved)

    @staticmethod
    def _submission_public(row: sqlite3.Row, *, admin: bool = False) -> dict:
        result = {
            "id": row["id"], "status": row["status"], "snapshot": json.loads(row["snapshot_json"]),
            "content_hash": row["content_hash"], "ai_report": json.loads(row["ai_report_json"]) if row["ai_report_json"] else None,
            "reason": row["reason"], "public_entry_id": row["public_entry_id"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }
        if admin:
            result["owner_id"] = row["owner_id"]
        return result

    def list_submissions(self, context: SessionContext) -> list[dict]:
        self.authorize_workspace(context.user_id, context.workspace_id, "wiki.read")
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM submissions WHERE owner_id=? AND workspace_id=? ORDER BY created_at DESC",
                (context.user_id, context.workspace_id),
            ).fetchall()
        return [self._submission_public(row) for row in rows]

    def get_submission(self, context: SessionContext, submission_id: str) -> dict:
        self.authorize_workspace(context.user_id, context.workspace_id, "wiki.read")
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM submissions WHERE id=? AND owner_id=? AND workspace_id=?",
                (submission_id, context.user_id, context.workspace_id),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(submission_id)
        return self._submission_public(row)

    @staticmethod
    def _legacy_snapshot_matches_identity(snapshot: dict, article: dict) -> bool:
        return (
            normalize_text(snapshot.get("title")).casefold() == normalize_text(article.get("title")).casefold()
            and normalize_text(snapshot.get("category")) == normalize_text(article.get("category"))
            and normalize_text(snapshot.get("content_status")) == normalize_text(article.get("content_status"))
        )

    @staticmethod
    def _publication_identity(value: dict) -> tuple[str, str, str]:
        return (
            normalize_text(value.get("title")).casefold(),
            normalize_text(value.get("category")),
            normalize_text(value.get("content_status")),
        )

    def backfill_workspace_publication_sources(self, context: SessionContext, articles: list[dict]) -> dict:
        """Bind legacy publication rows only when the private identity is unambiguous."""
        self.authorize_workspace(context.user_id, context.workspace_id, "wiki.read")
        private_by_identity: dict[tuple[str, str, str], list[dict]] = {}
        for article in articles:
            article_id = str(article.get("article_id") or "")
            if re.fullmatch(r"[a-f0-9]{32}", article_id):
                private_by_identity.setdefault(self._publication_identity(article), []).append(article)

        bound_entries = 0
        bound_submissions = 0
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_workspace_in_transaction(
                db, context.user_id, context.workspace_id, "wiki.read",
            )
            public_rows = db.execute("""
                SELECT entry.id,revision.snapshot_json
                FROM public_entries entry
                JOIN public_revisions revision ON revision.id=entry.current_revision_id
                JOIN submissions submission ON submission.id=revision.submission_id
                WHERE entry.author_id=? AND submission.workspace_id=?
                  AND entry.source_workspace_id IS NULL AND entry.source_article_id IS NULL
            """, (context.user_id, context.workspace_id)).fetchall()
            public_by_identity: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
            for row in public_rows:
                try:
                    identity = self._publication_identity(json.loads(row["snapshot_json"]))
                except (TypeError, json.JSONDecodeError):
                    continue
                public_by_identity.setdefault(identity, []).append(row)

            for identity, rows in public_by_identity.items():
                private = private_by_identity.get(identity, [])
                if len(private) != 1 or len(rows) != 1:
                    continue
                result = db.execute("""
                    UPDATE public_entries SET source_workspace_id=?,source_article_id=?
                    WHERE id=? AND source_workspace_id IS NULL AND source_article_id IS NULL
                """, (context.workspace_id, private[0]["article_id"], rows[0]["id"]))
                bound_entries += result.rowcount

            submission_rows = db.execute("""
                SELECT id,snapshot_json FROM submissions
                WHERE owner_id=? AND workspace_id=? AND article_id IS NULL
            """, (context.user_id, context.workspace_id)).fetchall()
            for row in submission_rows:
                try:
                    private = private_by_identity.get(
                        self._publication_identity(json.loads(row["snapshot_json"])), []
                    )
                except (TypeError, json.JSONDecodeError):
                    continue
                if len(private) != 1:
                    continue
                result = db.execute(
                    "UPDATE submissions SET article_id=? WHERE id=? AND article_id IS NULL",
                    (private[0]["article_id"], row["id"]),
                )
                bound_submissions += result.rowcount
            db.commit()
        return {"public_entries": bound_entries, "submissions": bound_submissions}

    @staticmethod
    def _row_fingerprint(row: sqlite3.Row, snapshot: dict) -> str:
        return row["publication_fingerprint"] or snapshot_fingerprint(snapshot)

    def article_publication(self, context: SessionContext, article: dict) -> dict:
        """Return the current user's publication state without exposing another tenant."""
        self.authorize_workspace(context.user_id, context.workspace_id, "wiki.read")
        article_id = str(article.get("article_id") or "")
        current_fingerprint = snapshot_fingerprint({
            "title": article.get("title"),
            "category": article.get("category"),
            "content_status": article.get("content_status"),
            "markdown": article.get("markdown", ""),
        })
        public_row = None
        active_row = None
        with self.connect() as db:
            public_candidates = db.execute("""
                SELECT e.id,e.status,e.moderation_reason,e.moderated_at,
                       e.source_article_id,r.id revision_id,r.version,r.snapshot_json,r.published_at,
                       r.publication_fingerprint
                FROM public_entries e
                JOIN public_revisions r ON r.id=e.current_revision_id
                JOIN submissions s ON s.id=r.submission_id
                WHERE e.author_id=? AND s.workspace_id=? AND e.status IN ('published','removed_by_admin')
                ORDER BY r.published_at DESC
            """, (context.user_id, context.workspace_id)).fetchall()
            submission_candidates = db.execute("""
                SELECT id,status,snapshot_json,updated_at,article_id,publication_fingerprint FROM submissions
                WHERE owner_id=? AND workspace_id=? ORDER BY updated_at DESC
            """, (context.user_id, context.workspace_id)).fetchall()

        exact_public = [row for row in public_candidates if row["source_article_id"] == article_id]
        if len(exact_public) == 1:
            public_row = (exact_public[0], json.loads(exact_public[0]["snapshot_json"]))

        active_candidates = [row for row in submission_candidates if row["status"] in ACTIVE_SUBMISSION_STATES]
        exact_active = [row for row in active_candidates if row["article_id"] == article_id]
        if len(exact_active) == 1:
            active_row = (exact_active[0], json.loads(exact_active[0]["snapshot_json"]))

        public_matches = bool(public_row and self._row_fingerprint(public_row[0], public_row[1]) == current_fingerprint)
        submission_matches = bool(active_row and self._row_fingerprint(active_row[0], active_row[1]) == current_fingerprint)
        public_status = public_row[0]["status"] if public_row else None
        if public_status == "removed_by_admin" and active_row:
            state = "relist_pending"
        elif public_status == "removed_by_admin" and public_matches:
            state = "removed"
        elif public_status == "removed_by_admin":
            state = "relist_available"
        elif public_matches:
            state = "published"
        elif active_row and public_row:
            state = "update_pending"
        elif public_row:
            state = "update_available"
        elif active_row:
            state = "submitted"
        else:
            state = "not_published"

        public = public_row[0] if public_row else None
        submission = active_row[0] if active_row else None
        return {
            "state": state,
            "public_entry_id": public["id"] if public else None,
            "public_revision_id": public["revision_id"] if public else None,
            "public_version": public["version"] if public else None,
            "published_at": public["published_at"] if public else None,
            "submission_id": submission["id"] if submission else None,
            "submission_status": submission["status"] if submission else None,
            "submission_matches_current": submission_matches,
            "publication_fingerprint": current_fingerprint,
            "moderation_reason": public["moderation_reason"] if public else None,
            "moderated_at": public["moderated_at"] if public else None,
        }

    def ai_decide(self, submission_id: str, decision: str, report: dict) -> dict:
        mapping = {"pass": "pending_admin", "needs_revision": "needs_revision", "reject": "ai_rejected", "failed": "ai_failed"}
        if decision not in mapping:
            raise ValueError("invalid AI review decision")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
            if row is None:
                db.rollback(); raise FileNotFoundError(submission_id)
            if row["status"] not in {"ai_queued", "ai_reviewing", "ai_failed"}:
                db.rollback(); raise RuntimeError("submission is not awaiting AI review")
            db.execute("UPDATE submissions SET status=?,ai_report_json=?,updated_at=? WHERE id=?", (
                mapping[decision], json.dumps({**report, "decision": decision}, ensure_ascii=False), now_iso(), submission_id,
            ))
            db.commit()
        self.audit(None, "submission.ai_review", "submission", submission_id, {"decision": decision})
        return {"id": submission_id, "status": mapping[decision]}

    def claim_ai_submission(self) -> dict | None:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM submissions WHERE status='ai_queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                db.commit()
                return None
            changed = db.execute("UPDATE submissions SET status='ai_reviewing',updated_at=? WHERE id=? AND status='ai_queued'", (now_iso(), row["id"])).rowcount
            db.commit()
        if not changed:
            return None
        return {
            "id": row["id"], "workspace_id": row["workspace_id"],
            "snapshot": json.loads(row["snapshot_json"]), "content_hash": row["content_hash"],
        }

    def retry_ai(self, context: SessionContext, submission_id: str) -> dict:
        self.authorize_workspace(context.user_id, context.workspace_id, "wiki.write")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_workspace_in_transaction(
                db, context.user_id, context.workspace_id, "wiki.write",
            )
            row = db.execute(
                "SELECT status FROM submissions WHERE id=? AND owner_id=? AND workspace_id=?",
                (submission_id, context.user_id, context.workspace_id),
            ).fetchone()
            if row is None:
                db.rollback()
                raise FileNotFoundError(submission_id)
            if row["status"] != "ai_failed":
                db.rollback()
                raise RuntimeError("submission cannot be retried")
            db.execute(
                "UPDATE submissions SET status='ai_queued',ai_report_json=NULL,updated_at=? WHERE id=?",
                (now_iso(), submission_id),
            )
            saved = db.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
            db.commit()
        return self._submission_public(saved)

    def withdraw(self, context: SessionContext, submission_id: str) -> dict:
        self.authorize_workspace(context.user_id, context.workspace_id, "wiki.write")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_workspace_in_transaction(
                db, context.user_id, context.workspace_id, "wiki.write",
            )
            row = db.execute(
                "SELECT * FROM submissions WHERE id=? AND owner_id=? AND workspace_id=?",
                (submission_id, context.user_id, context.workspace_id),
            ).fetchone()
            if row is None:
                db.rollback(); raise FileNotFoundError(submission_id)
            if row["status"] == "withdrawn":
                db.commit(); return self._submission_public(row)
            if row["status"] == "approved" and row["public_entry_id"]:
                db.execute("UPDATE public_entries SET status='withdrawn_by_author',updated_at=? WHERE id=?", (now_iso(), row["public_entry_id"]))
            db.execute("UPDATE submissions SET status='withdrawn',updated_at=? WHERE id=?", (now_iso(), submission_id))
            saved = db.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
            self._audit(db, context.user_id, "submission.withdraw", "submission", submission_id)
            db.commit()
        return self._submission_public(saved)

    def admin_list(self, context: SessionContext, status: str = "pending_admin") -> list[dict]:
        if context.role != "admin":
            raise PermissionError("admin role required")
        if status not in SUBMISSION_STATES:
            raise ValueError("invalid submission status")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM submissions WHERE status=? ORDER BY created_at", (status,)).fetchall()
        return [self._submission_public(row, admin=True) for row in rows]

    def admin_get(self, context: SessionContext, submission_id: str) -> dict:
        if context.role != "admin":
            raise PermissionError("admin role required")
        with self.connect() as db:
            row = db.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(submission_id)
        return self._submission_public(row, admin=True)

    def admin_decide(self, context: SessionContext, submission_id: str, decision: str, reason: str) -> dict:
        if context.role != "admin":
            raise PermissionError("admin role required")
        if decision not in {"approve", "request_changes", "reject"} or not reason.strip():
            raise ValueError("a valid decision and reason are required")
        now = now_iso()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_admin_in_transaction(db, context.user_id)
            row = db.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
            if row is None:
                db.rollback(); raise FileNotFoundError(submission_id)
            if row["status"] != "pending_admin":
                db.rollback(); raise RuntimeError("submission has already been decided")
            self_review = row["owner_id"] == context.user_id
            if decision == "approve":
                entry_id = None
                version = 1
                snapshot = json.loads(row["snapshot_json"])
                article_id = row["article_id"]
                if not isinstance(article_id, str) or re.fullmatch(r"[a-f0-9]{32}", article_id) is None:
                    db.rollback()
                    raise RuntimeError("legacy submission identity is unresolved")
                candidates = db.execute("""
                    SELECT e.id,e.status,e.current_revision_id,e.source_article_id,
                           r.version,r.snapshot_json
                    FROM public_entries e
                    JOIN public_revisions r ON r.id=e.current_revision_id
                    JOIN submissions prior ON prior.id=r.submission_id
                    WHERE e.author_id=? AND prior.workspace_id=?
                """, (row["owner_id"], row["workspace_id"])).fetchall()
                previous_public_status = None
                exact = [candidate for candidate in candidates if article_id and candidate["source_article_id"] == article_id]
                if len(exact) > 1:
                    db.rollback()
                    raise RuntimeError("multiple public entries share the same article identity")
                selected = exact[0] if exact else None
                if selected is None:
                    legacy = [
                        candidate for candidate in candidates
                        if candidate["source_article_id"] is None
                        and self._legacy_snapshot_matches_identity(json.loads(candidate["snapshot_json"]), snapshot)
                    ]
                    if legacy:
                        db.rollback()
                        raise RuntimeError("legacy public entry identity is unresolved")
                if selected is not None:
                    entry_id, version = selected["id"], selected["version"] + 1
                    previous_public_status = selected["status"]
                revision_id = uuid.uuid4().hex
                if entry_id is None:
                    entry_id = uuid.uuid4().hex
                    db.execute("""
                        INSERT INTO public_entries(
                            id,author_id,status,current_revision_id,created_at,updated_at,
                            source_workspace_id,source_article_id
                        ) VALUES(?,?,?,?,?,?,?,?)
                    """, (
                        entry_id, row["owner_id"], "published", revision_id, now, now,
                        row["workspace_id"], article_id,
                    ))
                else:
                    db.execute("""
                        UPDATE public_entries SET status='published',current_revision_id=?,updated_at=?,
                          moderation_reason=NULL,moderated_by=NULL,moderated_at=NULL WHERE id=?
                    """, (revision_id, now, entry_id))
                db.execute("""
                    INSERT INTO public_revisions(
                        id,entry_id,submission_id,version,snapshot_json,content_hash,published_at,
                        publication_fingerprint
                    ) VALUES(?,?,?,?,?,?,?,?)
                """, (
                    revision_id, entry_id, submission_id, version, row["snapshot_json"],
                    row["content_hash"], now,
                    row["publication_fingerprint"] or snapshot_fingerprint(snapshot),
                ))
                status = "approved"
                db.execute("UPDATE submissions SET status=?,reason=?,reviewer_id=?,public_entry_id=?,updated_at=? WHERE id=?", (
                    status, reason.strip(), context.user_id, entry_id, now, submission_id,
                ))
                if previous_public_status == "removed_by_admin":
                    self._notify(
                        db, row["owner_id"], "public_relisted", "public_entry", entry_id,
                        f"《{snapshot.get('title', '词条')}》已重新上架",
                        "你修改后提交的版本已通过审核并重新发布到 Wiki 广场。",
                    )
            else:
                status = "admin_changes_requested" if decision == "request_changes" else "admin_rejected"
                entry_id = None
                db.execute("UPDATE submissions SET status=?,reason=?,reviewer_id=?,updated_at=? WHERE id=?", (
                    status, reason.strip(), context.user_id, now, submission_id,
                ))
            self._audit(db, context.user_id, f"submission.{decision}", "submission", submission_id, {
                "reason": reason.strip(), "self_review": self_review,
            })
            db.commit()
        return {"id": submission_id, "status": status, "public_entry_id": entry_id}

    def list_public(self, query: str = "", category: str = "") -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT e.id,e.status,e.author_id,r.id revision_id,r.version,r.snapshot_json,r.content_hash,r.published_at,u.nickname
                FROM public_entries e JOIN public_revisions r ON r.id=e.current_revision_id JOIN users u ON u.id=e.author_id
                WHERE e.status='published' ORDER BY r.published_at DESC
            """).fetchall()
        result = []
        needle = query.strip().casefold()
        for row in rows:
            snapshot = json.loads(row["snapshot_json"])
            if category and snapshot.get("category") != category:
                continue
            if needle and needle not in (snapshot.get("title", "") + " " + snapshot.get("markdown", "")).casefold():
                continue
            result.append({
                "id": row["id"], "revision_id": row["revision_id"], "version": row["version"],
                "title": snapshot.get("title"), "category": snapshot.get("category"), "attribution": snapshot.get("attribution") or row["nickname"],
                "summary": snapshot.get("summary", ""), "published_at": row["published_at"], "content_hash": row["content_hash"],
            })
        return result

    def get_public(self, entry_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("""
                SELECT e.id,r.id revision_id,r.version,r.snapshot_json,r.content_hash,r.published_at,u.nickname
                FROM public_entries e JOIN public_revisions r ON r.id=e.current_revision_id JOIN users u ON u.id=e.author_id
                WHERE e.id=? AND e.status='published'
            """, (entry_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(entry_id)
        snapshot = json.loads(row["snapshot_json"])
        return {
            "id": row["id"], "revision_id": row["revision_id"], "version": row["version"],
            "snapshot": snapshot, "attribution": snapshot.get("attribution") or row["nickname"],
            "published_at": row["published_at"], "content_hash": row["content_hash"],
        }

    def report_public(self, entry_id: str, reporter_id: str | None, reason_code: str, detail: str) -> dict:
        if not reason_code.strip() or len(reason_code) > 40 or len(detail) > 1000:
            raise ValueError("invalid report")
        self.get_public(entry_id)
        report_id, now = uuid.uuid4().hex, now_iso()
        with self.connect() as db:
            db.execute("INSERT INTO reports VALUES(?,?,?,?,?,'open',NULL,NULL,?,?)", (
                report_id, entry_id, reporter_id, reason_code.strip(), detail.strip(), now, now,
            ))
        self.audit(reporter_id, "public.report", "public_entry", entry_id, {"report_id": report_id})
        return {"id": report_id, "status": "open"}

    def admin_reports(self, context: SessionContext) -> list[dict]:
        if context.role != "admin":
            raise PermissionError("admin role required")
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM reports WHERE status='open' ORDER BY created_at").fetchall()]

    def admin_public_entries(self, context: SessionContext, status: str) -> list[dict]:
        if context.role != "admin":
            raise PermissionError("admin role required")
        if status not in {"published", "removed_by_admin"}:
            raise ValueError("invalid public entry status")
        with self.connect() as db:
            rows = db.execute("""
                SELECT e.id,e.status,e.author_id,e.moderation_reason,e.moderated_at,
                       r.id revision_id,r.version,r.snapshot_json,r.content_hash,r.published_at,u.nickname
                FROM public_entries e JOIN public_revisions r ON r.id=e.current_revision_id
                JOIN users u ON u.id=e.author_id WHERE e.status=? ORDER BY e.updated_at DESC
            """, (status,)).fetchall()
        result = []
        for row in rows:
            snapshot = json.loads(row["snapshot_json"])
            result.append({
                "id": row["id"], "status": row["status"], "author_id": row["author_id"],
                "author_nickname": row["nickname"], "revision_id": row["revision_id"],
                "version": row["version"], "snapshot": snapshot, "content_hash": row["content_hash"],
                "published_at": row["published_at"], "moderation_reason": row["moderation_reason"],
                "moderated_at": row["moderated_at"],
            })
        return result

    def remove_public(self, context: SessionContext, entry_id: str, reason: str) -> dict:
        if context.role != "admin":
            raise PermissionError("admin role required")
        if not reason.strip():
            raise ValueError("a takedown reason is required")
        now = now_iso()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_admin_in_transaction(db, context.user_id)
            row = db.execute("""
                SELECT e.author_id,r.snapshot_json FROM public_entries e
                JOIN public_revisions r ON r.id=e.current_revision_id
                WHERE e.id=? AND e.status='published'
            """, (entry_id,)).fetchone()
            if row is None:
                db.rollback(); raise FileNotFoundError(entry_id)
            db.execute("""
                UPDATE public_entries SET status='removed_by_admin',updated_at=?,moderation_reason=?,
                  moderated_by=?,moderated_at=? WHERE id=?
            """, (now, reason.strip(), context.user_id, now, entry_id))
            title = json.loads(row["snapshot_json"]).get("title", "词条")
            self._notify(
                db, row["author_id"], "public_removed", "public_entry", entry_id,
                f"《{title}》已从 Wiki 广场下架",
                f"处理理由：{reason.strip()}。请修改私有正本后申请重新上架。",
            )
            self._audit(
                db, context.user_id, "public.remove", "public_entry", entry_id,
                {"reason": reason.strip()},
            )
            db.commit()
        return {"id": entry_id, "status": "removed_by_admin"}

    def relist_public(self, context: SessionContext, entry_id: str, reason: str) -> dict:
        if context.role != "admin":
            raise PermissionError("admin role required")
        if not reason.strip():
            raise ValueError("a relist reason is required")
        now = now_iso()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_admin_in_transaction(db, context.user_id)
            row = db.execute("""
                SELECT e.author_id,r.snapshot_json FROM public_entries e
                JOIN public_revisions r ON r.id=e.current_revision_id
                WHERE e.id=? AND e.status='removed_by_admin'
            """, (entry_id,)).fetchone()
            if row is None:
                db.rollback(); raise FileNotFoundError(entry_id)
            db.execute("""
                UPDATE public_entries SET status='published',updated_at=?,moderation_reason=NULL,
                  moderated_by=?,moderated_at=? WHERE id=?
            """, (now, context.user_id, now, entry_id))
            title = json.loads(row["snapshot_json"]).get("title", "词条")
            self._notify(
                db, row["author_id"], "public_relisted", "public_entry", entry_id,
                f"《{title}》已重新上架",
                f"Admin 已恢复该公开版本。处理说明：{reason.strip()}",
            )
            self._audit(
                db, context.user_id, "public.relist", "public_entry", entry_id,
                {"reason": reason.strip()},
            )
            db.commit()
        return {"id": entry_id, "status": "published"}

    def decide_report(self, context: SessionContext, report_id: str, action: str, reason: str) -> dict:
        if context.role != "admin" or action not in {"dismiss", "remove"} or not reason.strip():
            raise PermissionError("admin role, valid action, and reason are required")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_admin_in_transaction(db, context.user_id)
            row = db.execute("""
                SELECT reports.*,e.author_id,e.status AS entry_status,r.snapshot_json FROM reports
                JOIN public_entries e ON e.id=reports.entry_id
                JOIN public_revisions r ON r.id=e.current_revision_id
                WHERE reports.id=? AND reports.status='open'
            """, (report_id,)).fetchone()
            if row is None:
                db.rollback(); raise FileNotFoundError(report_id)
            if action == "remove":
                if row["entry_status"] != "published":
                    db.rollback(); raise RuntimeError("public entry is no longer published")
                now = now_iso()
                db.execute("""
                    UPDATE public_entries SET status='removed_by_admin',updated_at=?,moderation_reason=?,
                      moderated_by=?,moderated_at=? WHERE id=? AND status='published'
                """, (now, reason.strip(), context.user_id, now, row["entry_id"]))
                title = json.loads(row["snapshot_json"]).get("title", "词条")
                self._notify(
                    db, row["author_id"], "public_removed", "public_entry", row["entry_id"],
                    f"《{title}》因举报处理被下架",
                    f"处理理由：{reason.strip()}。请修改私有正本后申请重新上架。",
                )
            db.execute("UPDATE reports SET status='resolved',resolution=?,resolved_by=?,updated_at=? WHERE id=?", (
                f"{action}: {reason.strip()}", context.user_id, now_iso(), report_id,
            ))
            self._audit(
                db, context.user_id, f"report.{action}", "report", report_id,
                {"reason": reason.strip()},
            )
            db.commit()
        return {"id": report_id, "status": "resolved", "action": action}
