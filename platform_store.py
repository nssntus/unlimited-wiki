"""Platform identity, tenant metadata, encrypted secrets, and public review state."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
ROLES = {"user", "admin"}
SUBMISSION_STATES = {
    "ai_queued", "ai_reviewing", "ai_failed", "needs_revision", "ai_rejected",
    "pending_admin", "admin_changes_requested", "admin_rejected", "approved", "withdrawn",
}
PUBLIC_STATES = {"published", "withdrawn_by_author", "removed_by_admin", "superseded"}
ACTIVE_SUBMISSION_STATES = {"ai_queued", "ai_reviewing", "pending_admin"}


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
class SessionContext:
    user_id: str
    email: str
    nickname: str
    role: str
    workspace_id: str
    workspace_root_name: str
    workspace_name: str
    csrf_token: str
    expires_at: str

    def public(self) -> dict:
        return {
            "user": {"id": self.user_id, "email": self.email, "nickname": self.nickname, "role": self.role},
            "workspace": {"display_name": self.workspace_name},
            "csrf_token": self.csrf_token,
            "session_expires_at": self.expires_at,
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
        self._initialize()

    def connect(self) -> sqlite3.Connection:
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
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL UNIQUE REFERENCES users(id),
                    root_name TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    csrf_hash TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL
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
                    snapshot_json TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    status TEXT NOT NULL, snapshot_json TEXT NOT NULL, content_hash TEXT NOT NULL,
                    ai_report_json TEXT, reason TEXT, reviewer_id TEXT REFERENCES users(id), public_entry_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_submissions_owner ON submissions(owner_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status, created_at);
                CREATE TABLE IF NOT EXISTS public_entries (
                    id TEXT PRIMARY KEY, author_id TEXT NOT NULL REFERENCES users(id), status TEXT NOT NULL,
                    current_revision_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    moderation_reason TEXT, moderated_by TEXT REFERENCES users(id), moderated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS public_revisions (
                    id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES public_entries(id), submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id),
                    version INTEGER NOT NULL, snapshot_json TEXT NOT NULL, content_hash TEXT NOT NULL,
                    published_at TEXT NOT NULL, UNIQUE(entry_id, version)
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
            public_columns = {row["name"] for row in db.execute("PRAGMA table_info(public_entries)").fetchall()}
            for name, declaration in {
                "moderation_reason": "TEXT",
                "moderated_by": "TEXT REFERENCES users(id)",
                "moderated_at": "TEXT",
            }.items():
                if name not in public_columns:
                    db.execute(f"ALTER TABLE public_entries ADD COLUMN {name} {declaration}")
            db.execute("UPDATE submissions SET status='ai_queued',updated_at=? WHERE status='ai_reviewing'", (now_iso(),))
        os.chmod(self.db_path, 0o600)

    def audit(self, actor_id: str | None, action: str, object_type: str, object_id: str, detail: dict | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, actor_id, action, object_type, object_id, json.dumps(detail or {}, ensure_ascii=False), now_iso()),
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
        root_name = workspace_id
        recovery = secrets.token_urlsafe(24)
        created = now_iso()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            role = "admin" if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0 else "user"
            try:
                db.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)", (user_id, email, nickname, password_hash, role, "active", created))
                db.execute("INSERT INTO workspaces VALUES(?,?,?,?,?)", (workspace_id, user_id, root_name, f"{nickname} 的 Wiki", created))
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
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?)",
                (hash_token(token), user_id, hash_token(csrf), expires, now_iso()),
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
                       w.id workspace_id,w.root_name,w.display_name,
                       s.csrf_hash,s.expires_at
                FROM sessions s JOIN users u ON u.id=s.user_id JOIN workspaces w ON w.owner_id=u.id
                WHERE s.token_hash=? AND s.expires_at>?
            """, (hash_token(token), now_iso())).fetchone()
        if row is None or row["status"] != "active":
            return None
        csrf = self.vault.derive(token, scope="session-csrf")
        if not hmac.compare_digest(hash_token(csrf), row["csrf_hash"]):
            return None
        return SessionContext(
            row["user_id"], row["email"], row["nickname"], row["role"], row["workspace_id"],
            row["root_name"], row["display_name"], csrf, row["expires_at"],
        )

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

    def delete_account(self, context: SessionContext, password: str) -> None:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            user = db.execute("SELECT * FROM users WHERE id=? AND status='active'", (context.user_id,)).fetchone()
            if user is None or not verify_password(password, user["password_hash"]):
                db.rollback()
                raise ValueError("password is invalid")
            now = now_iso()
            db.execute("UPDATE public_entries SET status='withdrawn_by_author',updated_at=? WHERE author_id=? AND status='published'", (now, context.user_id))
            db.execute("DELETE FROM model_settings WHERE workspace_id=?", (context.workspace_id,))
            db.execute("DELETE FROM sessions WHERE user_id=?", (context.user_id,))
            db.execute("DELETE FROM recovery_codes WHERE user_id=?", (context.user_id,))
            db.execute("UPDATE users SET email=?,nickname='已注销用户',password_hash=?,status='deleted' WHERE id=?", (
                f"deleted-{context.user_id}@invalid.local", hash_password(secrets.token_urlsafe(32)), context.user_id,
            ))
            db.commit()
        self.audit(context.user_id, "user.delete", "user", context.user_id)

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
            row = db.execute("SELECT * FROM workspaces WHERE owner_id=?", (user_id,)).fetchone()
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

    def create_preview(self, context: SessionContext, article_path: str, source_revision: str, snapshot: dict) -> dict:
        preview_id = uuid.uuid4().hex
        canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        expires = future_iso(minutes=30)
        with self.connect() as db:
            db.execute("INSERT INTO share_previews VALUES(?,?,?,?,?,?,?,?,?)", (
                preview_id, context.user_id, context.workspace_id, article_path, source_revision,
                content_hash, canonical, expires, now_iso(),
            ))
        return {"preview_id": preview_id, "expires_at": expires, "source_revision": source_revision, "content_hash": content_hash, "snapshot": snapshot}

    def submit_preview(self, context: SessionContext, preview_id: str) -> dict:
        submission_id = uuid.uuid4().hex
        now = now_iso()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM share_previews WHERE id=? AND owner_id=? AND workspace_id=? AND expires_at>?", (
                preview_id, context.user_id, context.workspace_id, now,
            )).fetchone()
            if row is None:
                db.rollback()
                raise FileNotFoundError(preview_id)
            db.execute("INSERT INTO submissions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                submission_id, context.user_id, context.workspace_id, "ai_queued", row["snapshot_json"],
                row["content_hash"], None, None, None, None, now, now,
            ))
            db.execute("DELETE FROM share_previews WHERE id=?", (preview_id,))
            db.commit()
        self.audit(context.user_id, "submission.create", "submission", submission_id, {"content_hash": row["content_hash"]})
        return self.get_submission(context, submission_id)

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
        with self.connect() as db:
            rows = db.execute("SELECT * FROM submissions WHERE owner_id=? ORDER BY created_at DESC", (context.user_id,)).fetchall()
        return [self._submission_public(row) for row in rows]

    def get_submission(self, context: SessionContext, submission_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM submissions WHERE id=? AND owner_id=?", (submission_id, context.user_id)).fetchone()
        if row is None:
            raise FileNotFoundError(submission_id)
        return self._submission_public(row)

    @staticmethod
    def _snapshot_matches_article(snapshot: dict, article: dict) -> bool:
        return (
            str(snapshot.get("title", "")).strip() == str(article.get("title", "")).strip()
            and snapshot.get("category") == article.get("category")
            and snapshot.get("content_status") == article.get("content_status")
            and snapshot.get("markdown") == article.get("markdown")
        )

    def article_publication(self, context: SessionContext, article: dict) -> dict:
        """Return the current user's publication state without exposing another tenant."""
        title = str(article.get("title", "")).strip().casefold()
        public_row = None
        active_row = None
        with self.connect() as db:
            public_candidates = db.execute("""
                SELECT e.id,e.status,e.moderation_reason,e.moderated_at,
                       r.id revision_id,r.version,r.snapshot_json,r.published_at
                FROM public_entries e JOIN public_revisions r ON r.id=e.current_revision_id
                WHERE e.author_id=? AND e.status IN ('published','removed_by_admin')
                ORDER BY r.published_at DESC
            """, (context.user_id,)).fetchall()
            submission_candidates = db.execute("""
                SELECT id,status,snapshot_json,updated_at FROM submissions
                WHERE owner_id=? ORDER BY updated_at DESC
            """, (context.user_id,)).fetchall()

        for row in public_candidates:
            snapshot = json.loads(row["snapshot_json"])
            if str(snapshot.get("title", "")).strip().casefold() == title:
                public_row = (row, snapshot)
                break
        for row in submission_candidates:
            if row["status"] not in ACTIVE_SUBMISSION_STATES:
                continue
            snapshot = json.loads(row["snapshot_json"])
            if str(snapshot.get("title", "")).strip().casefold() == title:
                active_row = (row, snapshot)
                break

        public_matches = bool(public_row and self._snapshot_matches_article(public_row[1], article))
        submission_matches = bool(active_row and self._snapshot_matches_article(active_row[1], article))
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
        with self.connect() as db:
            changed = db.execute("UPDATE submissions SET status='ai_queued',ai_report_json=NULL,updated_at=? WHERE id=? AND owner_id=? AND status='ai_failed'", (
                now_iso(), submission_id, context.user_id,
            )).rowcount
        if not changed:
            raise RuntimeError("submission cannot be retried")
        return self.get_submission(context, submission_id)

    def withdraw(self, context: SessionContext, submission_id: str) -> dict:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM submissions WHERE id=? AND owner_id=?", (submission_id, context.user_id)).fetchone()
            if row is None:
                db.rollback(); raise FileNotFoundError(submission_id)
            if row["status"] == "withdrawn":
                db.commit(); return self._submission_public(row)
            if row["status"] == "approved" and row["public_entry_id"]:
                db.execute("UPDATE public_entries SET status='withdrawn_by_author',updated_at=? WHERE id=?", (now_iso(), row["public_entry_id"]))
            db.execute("UPDATE submissions SET status='withdrawn',updated_at=? WHERE id=?", (now_iso(), submission_id))
            db.commit()
        self.audit(context.user_id, "submission.withdraw", "submission", submission_id)
        return self.get_submission(context, submission_id)

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
            row = db.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
            if row is None:
                db.rollback(); raise FileNotFoundError(submission_id)
            if row["status"] != "pending_admin":
                db.rollback(); raise RuntimeError("submission has already been decided")
            self_review = row["owner_id"] == context.user_id
            if decision == "approve":
                entry_id = None
                version = 1
                title = str(json.loads(row["snapshot_json"]).get("title", "")).strip().casefold()
                candidates = db.execute("""
                    SELECT e.id,e.status,e.current_revision_id,r.version,r.snapshot_json FROM public_entries e
                    JOIN public_revisions r ON r.id=e.current_revision_id WHERE e.author_id=?
                """, (row["owner_id"],)).fetchall()
                previous_public_status = None
                for candidate in candidates:
                    if str(json.loads(candidate["snapshot_json"]).get("title", "")).strip().casefold() == title:
                        entry_id, version = candidate["id"], candidate["version"] + 1
                        previous_public_status = candidate["status"]
                        break
                revision_id = uuid.uuid4().hex
                if entry_id is None:
                    entry_id = uuid.uuid4().hex
                    db.execute("""
                        INSERT INTO public_entries(id,author_id,status,current_revision_id,created_at,updated_at)
                        VALUES(?,?,?,?,?,?)
                    """, (entry_id, row["owner_id"], "published", revision_id, now, now))
                else:
                    db.execute("""
                        UPDATE public_entries SET status='published',current_revision_id=?,updated_at=?,
                          moderation_reason=NULL,moderated_by=NULL,moderated_at=NULL WHERE id=?
                    """, (revision_id, now, entry_id))
                db.execute("INSERT INTO public_revisions VALUES(?,?,?,?,?,?,?)", (
                    revision_id, entry_id, submission_id, version, row["snapshot_json"], row["content_hash"], now,
                ))
                status = "approved"
                db.execute("UPDATE submissions SET status=?,reason=?,reviewer_id=?,public_entry_id=?,updated_at=? WHERE id=?", (
                    status, reason.strip(), context.user_id, entry_id, now, submission_id,
                ))
                if previous_public_status == "removed_by_admin":
                    snapshot = json.loads(row["snapshot_json"])
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
            db.commit()
        self.audit(context.user_id, f"submission.{decision}", "submission", submission_id, {
            "reason": reason.strip(), "self_review": self_review,
        })
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
            db.commit()
        self.audit(context.user_id, "public.remove", "public_entry", entry_id, {"reason": reason.strip()})
        return {"id": entry_id, "status": "removed_by_admin"}

    def relist_public(self, context: SessionContext, entry_id: str, reason: str) -> dict:
        if context.role != "admin":
            raise PermissionError("admin role required")
        if not reason.strip():
            raise ValueError("a relist reason is required")
        now = now_iso()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
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
            db.commit()
        self.audit(context.user_id, "public.relist", "public_entry", entry_id, {"reason": reason.strip()})
        return {"id": entry_id, "status": "published"}

    def decide_report(self, context: SessionContext, report_id: str, action: str, reason: str) -> dict:
        if context.role != "admin" or action not in {"dismiss", "remove"} or not reason.strip():
            raise PermissionError("admin role, valid action, and reason are required")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
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
            db.commit()
        self.audit(context.user_id, f"report.{action}", "report", report_id, {"reason": reason.strip()})
        return {"id": report_id, "status": "resolved", "action": action}
