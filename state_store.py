"""Local SQLite state for jobs, idempotency, Raw intake, and diagnostics."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, project_root: Path, *, recover_running: bool = True):
        self.project_root = project_root.resolve()
        state = project_root / ".wiki-state"
        state.mkdir(parents=True, exist_ok=True)
        self.path = state / "state.sqlite3"
        self._lock = threading.RLock()
        self._init_schema(recover_running=recover_running)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _init_schema(self, *, recover_running: bool) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS idempotency (
                    endpoint TEXT NOT NULL,
                    key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_json TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(endpoint, key)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    active_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_run_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS raw_records (
                    path TEXT PRIMARY KEY,
                    byte_hash TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    target_path TEXT,
                    disposition TEXT NOT NULL,
                    operation_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS raw_records_hash ON raw_records(byte_hash);
                CREATE TABLE IF NOT EXISTS classification_suggestions (
                    article_id TEXT NOT NULL,
                    article_revision TEXT NOT NULL,
                    taxonomy_revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    suggestion_json TEXT,
                    task_id TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(article_id, article_revision, taxonomy_revision)
                );
                CREATE TABLE IF NOT EXISTS raw_classification_plans (
                    raw_path TEXT NOT NULL,
                    raw_revision TEXT NOT NULL,
                    taxonomy_revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    plan_json TEXT,
                    task_id TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(raw_path, raw_revision, taxonomy_revision)
                );
                CREATE TABLE IF NOT EXISTS classification_previews (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS classification_draft (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reconciliation_items (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            task_columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            if "actor_user_id" not in task_columns:
                db.execute("ALTER TABLE tasks ADD COLUMN actor_user_id TEXT")
            if "paused_from_status" not in task_columns:
                db.execute("ALTER TABLE tasks ADD COLUMN paused_from_status TEXT")
            db.execute("DROP INDEX IF EXISTS tasks_one_active")
            db.execute(
                "CREATE UNIQUE INDEX tasks_one_active ON tasks(active_key) WHERE status IN ('staged','queued','running','paused')"
            )
            if recover_running:
                db.execute(
                    "UPDATE tasks SET status='queued', next_run_at=?, updated_at=? WHERE status='running'",
                    (now_iso(), now_iso()),
                )
            db.execute(
                """
                UPDATE tasks
                SET status='failed', error_type='feature_removed',
                    error_message='AI classification was retired; choose taxonomy while saving.',
                    next_run_at=NULL, paused_from_status=NULL, updated_at=?
                WHERE kind IN ('article-classification','raw-classification-plan')
                  AND status IN ('staged','queued','running','paused')
                """,
                (now_iso(),),
            )
            db.execute("DELETE FROM idempotency WHERE status='pending'")
            staged = db.execute("SELECT id,payload_json FROM tasks WHERE status='staged'").fetchall()
            for row in staged:
                payload = json.loads(row["payload_json"])
                path = payload.get("path")
                if isinstance(path, str) and (self.project_root / "wiki" / path).is_file():
                    db.execute("UPDATE tasks SET status='queued', next_run_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), row["id"]))
                else:
                    db.execute("DELETE FROM tasks WHERE id=?", (row["id"],))

    @staticmethod
    def payload_hash(payload: dict) -> str:
        packed = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(packed.encode("utf-8")).hexdigest()

    def idempotency_get(self, endpoint: str, key: str, payload: dict) -> dict | None:
        digest = self.payload_hash(payload)
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM idempotency WHERE endpoint=? AND key=?", (endpoint, key)
            ).fetchone()
        if not row:
            return None
        if row["payload_hash"] != digest:
            raise ValueError("idempotency key was already used with a different payload")
        if row["status"] == "done" and row["response_json"]:
            return json.loads(row["response_json"])
        raise RuntimeError("request with this idempotency key is still in progress")

    def idempotency_begin(self, endpoint: str, key: str, payload: dict) -> bool:
        digest = self.payload_hash(payload)
        try:
            with self.connect() as db:
                db.execute(
                    "INSERT INTO idempotency(endpoint,key,payload_hash,status,created_at) VALUES(?,?,?,?,?)",
                    (endpoint, key, digest, "pending", now_iso()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def idempotency_finish(self, endpoint: str, key: str, response: dict) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE idempotency SET status='done', response_json=? WHERE endpoint=? AND key=?",
                (json.dumps(response, ensure_ascii=False), endpoint, key),
            )

    def idempotency_abort(self, endpoint: str, key: str) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM idempotency WHERE endpoint=? AND key=? AND status='pending'",
                (endpoint, key),
            )

    def enqueue_task(
        self, kind: str, subject: str, payload: dict, *, staged: bool = False,
        actor_user_id: str | None = None,
    ) -> tuple[dict, bool]:
        active_key = f"{kind}:{subject.casefold()}"
        task_id = uuid.uuid4().hex
        created = now_iso()
        with self._lock, self.connect() as db:
            existing = db.execute(
                "SELECT * FROM tasks WHERE active_key=? AND status IN ('staged','queued','running','paused')",
                (active_key,),
            ).fetchone()
            if existing:
                return self._task(existing), False
            status = "staged" if staged else "queued"
            db.execute(
                """INSERT INTO tasks
                (id,kind,subject,active_key,status,payload_json,attempts,next_run_at,created_at,updated_at,actor_user_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, kind, subject, active_key, status, json.dumps(payload, ensure_ascii=False), 0, created, created, created, actor_user_id),
            )
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task(row), True

    def activate_task(self, task_id: str) -> dict:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE tasks SET status='queued', next_run_at=?, updated_at=? WHERE id=? AND status='staged'",
                (now_iso(), now_iso(), task_id),
            ).rowcount
        if not changed:
            raise ValueError("task cannot be activated")
        return self.get_task(task_id)

    def delete_staged_task(self, task_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM tasks WHERE id=? AND status='staged'", (task_id,))

    def list_tasks(self, limit: int = 100) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._task(row) for row in rows]

    def get_task(self, task_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise FileNotFoundError(task_id)
        return self._task(row)

    def claim_task(self, allowed_kinds: set[str] | None = None) -> dict | None:
        now = now_iso()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            kind_clause = ""
            params: list[str] = [now]
            if allowed_kinds is not None:
                if not allowed_kinds:
                    db.commit()
                    return None
                placeholders = ",".join("?" for _ in allowed_kinds)
                kind_clause = f" AND kind IN ({placeholders})"
                params.extend(sorted(allowed_kinds))
            row = db.execute(
                "SELECT * FROM tasks WHERE status='queued' "
                "AND (next_run_at IS NULL OR next_run_at<=?)"
                + kind_clause
                + " ORDER BY created_at LIMIT 1",
                params,
            ).fetchone()
            if not row:
                db.commit()
                return None
            changed = db.execute(
                "UPDATE tasks SET status='running', attempts=attempts+1, updated_at=? WHERE id=? AND status='queued'",
                (now, row["id"]),
            ).rowcount
            db.commit()
            if not changed:
                return None
        return self.get_task(row["id"])

    def complete_task(self, task_id: str, result: dict, *, expected_attempt: int | None = None) -> dict:
        attempt_clause = " AND attempts=?" if expected_attempt is not None else ""
        params: list[object] = [json.dumps(result, ensure_ascii=False), now_iso(), task_id]
        if expected_attempt is not None:
            params.append(expected_attempt)
        with self.connect() as db:
            db.execute(
                """UPDATE tasks SET status='succeeded', result_json=?, error_type=NULL,
                error_message=NULL, next_run_at=NULL, updated_at=? WHERE id=? AND status='running'"""
                + attempt_clause,
                params,
            )
        return self.get_task(task_id)

    def fail_task(self, task_id: str, error_type: str, message: str, *, retry: bool, expected_attempt: int | None = None) -> dict:
        task = self.get_task(task_id)
        backoff = (5, 30, 120, 600, 3600)
        attempts = task["attempts"]
        should_retry = retry and attempts < len(backoff)
        next_at = None
        status = "failed"
        if should_retry:
            status = "queued"
            next_at = (datetime.now(timezone.utc) + timedelta(seconds=backoff[attempts - 1])).isoformat(timespec="seconds")
        attempt_clause = " AND attempts=?" if expected_attempt is not None else ""
        params: list[object] = [status, error_type, message[:500], next_at, now_iso(), task_id]
        if expected_attempt is not None:
            params.append(expected_attempt)
        with self.connect() as db:
            db.execute(
                """UPDATE tasks SET status=?, error_type=?, error_message=?, next_run_at=?,
                updated_at=? WHERE id=? AND status='running'""" + attempt_clause,
                params,
            )
        return self.get_task(task_id)

    def finalize_classification_success(
        self,
        task_id: str,
        expected_attempt: int,
        result: dict,
        *,
        article_id: str,
        article_revision: str,
        taxonomy_revision: int,
        suggestion: dict,
    ) -> dict:
        stamp = now_iso()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                """UPDATE tasks SET status='succeeded', result_json=?, error_type=NULL,
                error_message=NULL, next_run_at=NULL, updated_at=?
                WHERE id=? AND status='running' AND attempts=?""",
                (json.dumps(result, ensure_ascii=False), stamp, task_id, expected_attempt),
            ).rowcount
            if changed:
                db.execute(
                    """INSERT INTO classification_suggestions
                    (article_id,article_revision,taxonomy_revision,status,suggestion_json,task_id,error_type,error_message,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(article_id,article_revision,taxonomy_revision) DO UPDATE SET
                    status=excluded.status,suggestion_json=excluded.suggestion_json,task_id=excluded.task_id,
                    error_type=NULL,error_message=NULL,updated_at=excluded.updated_at""",
                    (article_id, article_revision, taxonomy_revision, "succeeded",
                     json.dumps(suggestion, ensure_ascii=False), task_id, None, None, stamp, stamp),
                )
            db.commit()
        return self.get_task(task_id)

    def finalize_raw_classification_success(
        self,
        task_id: str,
        expected_attempt: int,
        result: dict,
        *,
        raw_path: str,
        raw_revision: str,
        taxonomy_revision: int,
        plan: dict,
    ) -> dict:
        stamp = now_iso()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                """UPDATE tasks SET status='succeeded', result_json=?, error_type=NULL,
                error_message=NULL, next_run_at=NULL, updated_at=?
                WHERE id=? AND status='running' AND attempts=?""",
                (json.dumps(result, ensure_ascii=False), stamp, task_id, expected_attempt),
            ).rowcount
            if changed:
                db.execute(
                    """INSERT INTO raw_classification_plans
                    (raw_path,raw_revision,taxonomy_revision,status,plan_json,task_id,error_type,error_message,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(raw_path,raw_revision,taxonomy_revision) DO UPDATE SET
                    status=excluded.status,plan_json=excluded.plan_json,task_id=excluded.task_id,
                    error_type=NULL,error_message=NULL,updated_at=excluded.updated_at""",
                    (raw_path, raw_revision, taxonomy_revision, "succeeded",
                     json.dumps(plan, ensure_ascii=False), task_id, None, None, stamp, stamp),
                )
            db.commit()
        return self.get_task(task_id)

    def cancel_task(self, task_id: str) -> dict:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE tasks SET status='cancelled', updated_at=? WHERE id=? AND status IN ('queued','running')",
                (now_iso(), task_id),
            ).rowcount
        if not changed and self.get_task(task_id)["status"] not in {"cancelled", "succeeded", "failed"}:
            raise ValueError("task cannot be cancelled")
        return self.get_task(task_id)

    def pause_active_tasks(self) -> list[dict]:
        stamp = now_iso()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id FROM tasks WHERE status IN ('staged','queued','running')"
            ).fetchall()
            task_ids = [row["id"] for row in rows]
            db.execute("""
                UPDATE tasks SET paused_from_status=status,status='paused',next_run_at=NULL,updated_at=?
                WHERE status IN ('staged','queued','running')
            """, (stamp,))
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                db.execute(
                    f"UPDATE classification_suggestions SET status='paused',updated_at=? WHERE task_id IN ({placeholders})",
                    [stamp, *task_ids],
                )
                db.execute(
                    f"UPDATE raw_classification_plans SET status='paused',updated_at=? WHERE task_id IN ({placeholders})",
                    [stamp, *task_ids],
                )
            db.commit()
        return [self.get_task(task_id) for task_id in task_ids]

    def resume_paused_tasks(self) -> list[dict]:
        stamp = now_iso()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id,paused_from_status FROM tasks WHERE status='paused'").fetchall()
            task_ids = [row["id"] for row in rows]
            for row in rows:
                status = "staged" if row["paused_from_status"] == "staged" else "queued"
                db.execute("""
                    UPDATE tasks SET status=?,paused_from_status=NULL,next_run_at=?,updated_at=?
                    WHERE id=? AND status='paused'
                """, (status, stamp if status == "queued" else None, stamp, row["id"]))
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                db.execute(
                    f"UPDATE classification_suggestions SET status='queued',updated_at=? WHERE task_id IN ({placeholders}) AND status='paused'",
                    [stamp, *task_ids],
                )
                db.execute(
                    f"UPDATE raw_classification_plans SET status='queued',updated_at=? WHERE task_id IN ({placeholders}) AND status='paused'",
                    [stamp, *task_ids],
                )
            db.commit()
        return [self.get_task(task_id) for task_id in task_ids]

    def terminate_workspace_tasks(self) -> list[dict]:
        stamp = now_iso()
        message = "The workspace was deleted."
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id FROM tasks WHERE status IN ('staged','queued','running','paused')"
            ).fetchall()
            task_ids = [row["id"] for row in rows]
            db.execute("""
                UPDATE tasks SET status='failed',paused_from_status=NULL,error_type='workspace_deleted',
                    error_message=?,next_run_at=NULL,updated_at=?
                WHERE status IN ('staged','queued','running','paused')
            """, (message, stamp))
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                params = [message, stamp, *task_ids]
                db.execute(
                    f"UPDATE classification_suggestions SET status='failed',error_type='workspace_deleted',error_message=?,updated_at=? WHERE task_id IN ({placeholders})",
                    params,
                )
                db.execute(
                    f"UPDATE raw_classification_plans SET status='failed',error_type='workspace_deleted',error_message=?,updated_at=? WHERE task_id IN ({placeholders})",
                    params,
                )
            db.commit()
        return [self.get_task(task_id) for task_id in task_ids]

    def retry_task(
        self, task_id: str, *, payload: dict | None = None, actor_user_id: str | None = None,
    ) -> dict:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(task_id)
            task = self._task(row)
            retryable_conflict = task["status"] == "succeeded" and bool(task.get("result", {}).get("conflict"))
            allowed_status = task["status"] in {"failed", "cancelled"} or retryable_conflict
            if not allowed_status:
                raise ValueError("task cannot be retried")
            next_payload = payload or task["payload"]
            changed = db.execute(
                """UPDATE tasks SET status='queued', payload_json=?, actor_user_id=?, result_json=NULL,
                error_type=NULL, error_message=NULL, next_run_at=?, updated_at=?
                WHERE id=? AND status=? AND attempts=?""",
                (
                    json.dumps(next_payload, ensure_ascii=False), actor_user_id, now_iso(), now_iso(),
                    task_id, task["status"], task["attempts"],
                ),
            ).rowcount
            db.commit()
        if not changed:
            raise ValueError("task cannot be retried")
        return self.get_task(task_id)

    def record_raw(
        self,
        path: str,
        byte_hash: str,
        text_hash: str,
        disposition: str,
        target_path: str | None,
        operation_id: str | None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO raw_records(path,byte_hash,text_hash,target_path,disposition,operation_id,created_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET byte_hash=excluded.byte_hash,text_hash=excluded.text_hash,
                target_path=excluded.target_path,disposition=excluded.disposition,
                operation_id=excluded.operation_id,created_at=excluded.created_at""",
                (path, byte_hash, text_hash, target_path, disposition, operation_id, now_iso()),
            )

    def raw_records(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM raw_records ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def remap_article_path(self, old_path: str, new_path: str, *, base_revision: str | None = None) -> None:
        """Keep Raw records and pending tasks aligned after a Wiki move."""
        if old_path == new_path:
            return
        with self._lock, self.connect() as db:
            db.execute("UPDATE raw_records SET target_path=? WHERE target_path=?", (new_path, old_path))
            rows = db.execute(
                "SELECT id,kind,payload_json FROM tasks WHERE status IN ('staged','queued')"
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"])
                if payload.get("path") != old_path:
                    continue
                payload["path"] = new_path
                if base_revision:
                    payload["base_revision"] = base_revision
                db.execute(
                    "UPDATE tasks SET subject=?,active_key=?,payload_json=?,updated_at=? WHERE id=?",
                    (
                        new_path,
                        f"{row['kind']}:{new_path.casefold()}",
                        json.dumps(payload, ensure_ascii=False),
                        now_iso(),
                        row["id"],
                    ),
                )

    def save_classification_suggestion(
        self,
        article_id: str,
        article_revision: str,
        taxonomy_revision: int,
        status: str,
        *,
        suggestion: dict | None = None,
        task_id: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> dict:
        stamp = now_iso()
        with self.connect() as db:
            db.execute(
                """INSERT INTO classification_suggestions
                (article_id,article_revision,taxonomy_revision,status,suggestion_json,task_id,error_type,error_message,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(article_id,article_revision,taxonomy_revision) DO UPDATE SET
                status=excluded.status,suggestion_json=excluded.suggestion_json,task_id=excluded.task_id,
                error_type=excluded.error_type,error_message=excluded.error_message,updated_at=excluded.updated_at""",
                (article_id, article_revision, taxonomy_revision, status,
                 json.dumps(suggestion, ensure_ascii=False) if suggestion is not None else None,
                 task_id, error_type, error_message[:500] if error_message else None, stamp, stamp),
            )
        return self.classification_suggestion(article_id, article_revision, taxonomy_revision) or {}

    def save_raw_classification_plan(self, raw_path: str, raw_revision: str, taxonomy_revision: int, status: str, *, plan: dict | None = None, task_id: str | None = None, error_type: str | None = None, error_message: str | None = None) -> dict:
        stamp = now_iso()
        with self.connect() as db:
            db.execute(
                """INSERT INTO raw_classification_plans
                (raw_path,raw_revision,taxonomy_revision,status,plan_json,task_id,error_type,error_message,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(raw_path,raw_revision,taxonomy_revision) DO UPDATE SET
                status=excluded.status,plan_json=excluded.plan_json,task_id=excluded.task_id,
                error_type=excluded.error_type,error_message=excluded.error_message,updated_at=excluded.updated_at""",
                (raw_path, raw_revision, taxonomy_revision, status, json.dumps(plan, ensure_ascii=False) if plan is not None else None, task_id, error_type, error_message[:500] if error_message else None, stamp, stamp),
            )
        return self.raw_classification_plan(raw_path, raw_revision, taxonomy_revision) or {}

    def raw_classification_plan(self, raw_path: str, raw_revision: str, taxonomy_revision: int) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM raw_classification_plans WHERE raw_path=? AND raw_revision=? AND taxonomy_revision=?",
                (raw_path, raw_revision, taxonomy_revision),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        raw_plan = item.pop("plan_json")
        item["plan"] = json.loads(raw_plan) if raw_plan else None
        return item

    def classification_suggestion(self, article_id: str, article_revision: str, taxonomy_revision: int) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM classification_suggestions WHERE article_id=? AND article_revision=? AND taxonomy_revision=?",
                (article_id, article_revision, taxonomy_revision),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        raw_suggestion = item.pop("suggestion_json")
        item["suggestion"] = json.loads(raw_suggestion) if raw_suggestion else None
        return item

    def create_preview(self, kind: str, payload: dict, *, ttl_minutes: int = 30) -> dict:
        preview_id = uuid.uuid4().hex
        created = now_iso()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute(
                "INSERT INTO classification_previews(id,kind,payload_json,expires_at,created_at) VALUES(?,?,?,?,?)",
                (preview_id, kind, json.dumps(payload, ensure_ascii=False), expires, created),
            )
        return {"preview_id": preview_id, "kind": kind, "payload": payload, "expires_at": expires}

    def get_preview(self, preview_id: str, kind: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM classification_previews WHERE id=? AND kind=? AND expires_at>=?",
                (preview_id, kind, now_iso()),
            ).fetchone()
        if not row:
            raise FileNotFoundError(preview_id)
        return {**dict(row), "payload": json.loads(row["payload_json"])}

    def consume_preview(self, preview_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM classification_previews WHERE id=?", (preview_id,))

    def classification_draft(self) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM classification_draft WHERE id=1").fetchone()
        if not row:
            return {"revision": 0, "selections": []}
        return {"revision": row["revision"], "selections": json.loads(row["payload_json"])}

    def save_classification_draft(self, selections: list[dict], expected_revision: int) -> dict:
        if not isinstance(selections, list) or len(selections) > 500:
            raise ValueError("invalid classification draft")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM classification_draft WHERE id=1").fetchone()
            current = row["revision"] if row else 0
            if current != expected_revision:
                db.rollback()
                raise RuntimeError("classification draft changed")
            next_revision = current + 1
            db.execute(
                """INSERT INTO classification_draft(id,revision,payload_json,updated_at) VALUES(1,?,?,?)
                ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                (next_revision, json.dumps(selections, ensure_ascii=False), now_iso()),
            )
            db.commit()
        return {"revision": next_revision, "selections": selections}

    def clear_classification_draft(self) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM classification_draft WHERE id=1")

    def replace_reconciliation(self, items: list[dict]) -> list[dict]:
        stamp = now_iso()
        fingerprints = [item["fingerprint"] for item in items]
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if fingerprints:
                placeholders = ",".join("?" for _ in fingerprints)
                db.execute(
                    f"DELETE FROM reconciliation_items WHERE status='pending' AND fingerprint NOT IN ({placeholders})",
                    fingerprints,
                )
            else:
                db.execute("DELETE FROM reconciliation_items WHERE status='pending'")
            for item in items:
                db.execute(
                    """INSERT INTO reconciliation_items(id,fingerprint,kind,payload_json,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET
                    kind=excluded.kind,payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                    (uuid.uuid4().hex, item["fingerprint"], item["kind"], json.dumps(item, ensure_ascii=False), "pending", stamp, stamp),
                )
            db.commit()
        return self.list_reconciliation()

    def list_reconciliation(self, status: str = "pending") -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM reconciliation_items WHERE status=? ORDER BY created_at", (status,)
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def get_reconciliation(self, item_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM reconciliation_items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise FileNotFoundError(item_id)
        return {**dict(row), "payload": json.loads(row["payload_json"])}

    def resolve_reconciliation(self, item_id: str, status: str) -> dict:
        if status not in {"adopted", "restored", "deferred"}:
            raise ValueError("invalid reconciliation status")
        with self.connect() as db:
            changed = db.execute(
                "UPDATE reconciliation_items SET status=?,updated_at=? WHERE id=? AND status='pending'",
                (status, now_iso(), item_id),
            ).rowcount
        if not changed:
            raise ValueError("reconciliation item is no longer pending")
        return self.get_reconciliation(item_id)

    @staticmethod
    def _task(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        raw_result = item.pop("result_json")
        item["result"] = json.loads(raw_result) if raw_result else None
        return item
