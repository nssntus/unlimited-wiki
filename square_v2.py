"""Square V2 public discovery, reuse, interaction, and curation store helpers."""

from __future__ import annotations

import base64
import difflib
import hashlib
import ipaddress
import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from markdown_metadata import META_RE, canonical_metadata_preamble


REPORT_REASONS = {
    "copyright", "privacy", "illegal_or_dangerous", "harassment_or_fraud", "spam", "other",
}
CORRECTION_KINDS = {"factual", "source", "outdated", "duplicate", "supplement", "clarity", "typo", "other"}
REUSE_PERMISSIONS = {"view_only", "allow_private_copy"}
REUSE_POLICY_VERSION = "square-reuse-v1"
PUBLIC_SORTS = {"relevance", "latest", "updated"}
PUBLIC_REVIEW_ISSUE_CODES = {
    "copyright", "privacy", "unsafe", "unsupported_claim", "source_quality",
    "spam", "duplicate", "policy_violation", "not_configured", "model_error",
    "invalid_model_response",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.strip().casefold()).strip("-")
    if not result or len(result) > 64:
        raise ValueError("invalid public slug")
    return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cursor(position: list[Any], signature: str) -> str:
    return base64.urlsafe_b64encode(_json({"position": position, "signature": signature}).encode()).decode().rstrip("=")


def _decode_cursor(value: str | None, signature: str) -> list[Any] | None:
    if not value:
        return None
    if len(value) > 500:
        raise ValueError("invalid cursor")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        decoded = json.loads(raw)
        position = decoded["position"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if decoded.get("signature") != signature:
        raise ValueError("cursor does not match the current search")
    if not isinstance(position, list):
        raise ValueError("invalid cursor")
    return position


def _search_terms(value: str) -> list[str]:
    """Produce deterministic tokens, including CJK substring n-grams."""
    normalized = value.casefold()
    result: list[str] = []
    for segment in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", normalized):
        if re.fullmatch(r"[\u3400-\u9fff]+", segment):
            for size in range(1, min(3, len(segment)) + 1):
                result.extend(segment[index:index + size] for index in range(len(segment) - size + 1))
        else:
            result.append(segment)
    return list(dict.fromkeys(result))


def _search_projection(value: str) -> str:
    return " ".join(_search_terms(value))


def canonical_public_url(value: str) -> str | None:
    if not isinstance(value, str) or value != value.strip() or len(value) > 1000:
        return None
    if any(ord(char) < 0x20 or char.isspace() for char in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    hostname = (parsed.hostname or "").rstrip(".")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return None
    if not hostname or hostname.casefold() == "localhost" or hostname.casefold().endswith((".localhost", ".local", ".internal")):
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global:
            return None
        canonical_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    else:
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None
        if not re.fullmatch(r"[a-z0-9.-]+", ascii_hostname) or ".." in ascii_hostname:
            return None
        # Browsers accept several non-canonical numeric IPv4 forms that
        # ipaddress deliberately rejects. Never treat them as DNS names.
        numeric_component = r"(?:0x[0-9a-f]+|[0-9]+)"
        if re.fullmatch(rf"{numeric_component}(?:\.{numeric_component})*", ascii_hostname, re.IGNORECASE):
            return None
        canonical_host = ascii_hostname
    if port is not None and not 1 <= port <= 65535:
        return None
    authority = canonical_host + (f":{port}" if port is not None else "")
    return urlunsplit((parsed.scheme, authority, parsed.path or "", parsed.query, parsed.fragment))


def _safe_sources(snapshot: dict) -> list[dict]:
    # Legacy source summaries can contain private paths. Only the author's
    # explicit public selection is eligible for a public revision.
    values = snapshot.get("public_sources", [])
    result: list[dict] = []
    if not isinstance(values, list):
        return result
    for item in values[:30]:
        if isinstance(item, dict):
            raw_url = str(item.get("url") or "")
        else:
            continue
        canonical = canonical_public_url(raw_url)
        if canonical:
            host = urlsplit(canonical).hostname or "外部来源"
            result.append({
                "label": host[:160], "url": canonical,
                "kind": str(item.get("kind") or "reference")[:40],
            })
    return result


def safe_public_url(value: str) -> bool:
    return canonical_public_url(value) is not None


def _public_review_issues(report: Any) -> list[dict]:
    """Project untrusted model output to stable, non-sensitive public facts."""
    if not isinstance(report, dict) or not isinstance(report.get("issues"), list):
        return []
    result: list[dict] = []
    for value in report["issues"][:20]:
        if not isinstance(value, dict):
            continue
        code = str(value.get("code") or "").strip().casefold()
        if code in PUBLIC_REVIEW_ISSUE_CODES and code not in {item["code"] for item in result}:
            result.append({"code": code})
    return result


def _square_public_markdown(markdown: str) -> str:
    """Remove the private canonical preamble while preserving body Markdown."""
    value = str(markdown or "")
    header = canonical_metadata_preamble(value)
    if header is not None:
        lines, _title_index, block_start, index = header
        visible = [
            line for line in lines[block_start:index]
            if META_RE.match(line.rstrip("\r\n")) is None
        ]
        value = "".join(lines[:block_start] + visible + lines[index:])
    # Early public snapshots were created after stable identity fields had
    # already been removed. Recognize their remaining generated preamble only
    # when the first quote block is anchored by both Category and Status.
    lines = value.splitlines(True)
    title_index = next((i for i, line in enumerate(lines) if line.lstrip("\ufeff \t").startswith("# ")), -1)
    cursor = title_index + 1
    while cursor > 0 and cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    block_start = cursor
    blocks: list[tuple[int, int, set[str]]] = []
    allowed = {
        "article-id", "category-id", "classification", "classification-updated",
        "category", "status", "generation", "updated", "archived", "tags",
        "evidence", "sources", "raw",
    }
    while cursor > 0 and cursor < len(lines):
        start = cursor
        keys: set[str] = set()
        while cursor < len(lines):
            match = META_RE.match(lines[cursor].rstrip("\r\n"))
            if match is None or match.group(1).strip().casefold() not in allowed:
                break
            keys.add(match.group(1).strip().casefold())
            cursor += 1
        if not keys:
            break
        blocks.append((start, cursor, keys))
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
    if blocks and {"category", "status"}.issubset(blocks[0][2]):
        value = "".join(lines[:block_start] + lines[cursor:])
    # Older generated articles carry private workflow state in this exact
    # trailing footer. Requiring the terminal horizontal rule keeps body text
    # with the same words untouched.
    return re.sub(
        r"\r?\n---[ \t]*\r?\n(?:[ \t]*\r?\n)*(?:\*(?:分类|状态)：[^\r\n]*\*[ \t]*(?:\r?\n|$)(?:[ \t]*\r?\n)*){1,2}\Z",
        "\n",
        value,
    )


def _public_snapshot(snapshot: dict) -> dict:
    """Return only fields approved for anonymous public delivery."""
    return {
        "title": str(snapshot.get("title") or "")[:300],
        # Private category labels are governed through the public taxonomy,
        # never copied from an immutable legacy snapshot.
        "category": "",
        "content_status": str(snapshot.get("content_status") or "")[:40],
        "markdown": _square_public_markdown(str(snapshot.get("markdown") or "")),
        "summary": str(snapshot.get("summary") or "")[:2000],
        "attribution": str(snapshot.get("attribution") or "匿名用户")[:120],
        "source_summaries": [],
    }


def initialize_square_schema(db: sqlite3.Connection) -> None:
    """Create additive Square V2 state without rewriting immutable snapshots."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS public_categories (
            id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
            sort_order INTEGER NOT NULL DEFAULT 0, created_by TEXT REFERENCES users(id),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS public_tags (
            id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS public_entry_tags (
            entry_id TEXT NOT NULL REFERENCES public_entries(id) ON DELETE CASCADE,
            tag_id TEXT NOT NULL REFERENCES public_tags(id),
            PRIMARY KEY(entry_id,tag_id)
        );
        CREATE TABLE IF NOT EXISTS public_category_mappings (
            private_label TEXT PRIMARY KEY, category_id TEXT REFERENCES public_categories(id),
            status TEXT NOT NULL DEFAULT 'pending', mapped_by TEXT REFERENCES users(id), updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS public_category_slug_redirects (
            slug TEXT PRIMARY KEY, category_id TEXT NOT NULL REFERENCES public_categories(id),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS public_reuse_permissions (
            entry_id TEXT PRIMARY KEY REFERENCES public_entries(id) ON DELETE CASCADE,
            permission TEXT NOT NULL DEFAULT 'view_only', granted_by TEXT REFERENCES users(id),
            granted_at TEXT, revoked_at TEXT, policy_version TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS public_imports (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            public_entry_id TEXT NOT NULL REFERENCES public_entries(id),
            public_revision_id TEXT NOT NULL REFERENCES public_revisions(id),
            private_article_id TEXT NOT NULL, private_path TEXT NOT NULL,
            status TEXT NOT NULL, imported_at TEXT, policy_version TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(workspace_id,public_revision_id)
        );
        CREATE TABLE IF NOT EXISTS public_subscriptions (
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            public_entry_id TEXT NOT NULL REFERENCES public_entries(id) ON DELETE CASCADE,
            status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id,public_entry_id)
        );
        CREATE TABLE IF NOT EXISTS correction_suggestions (
            id TEXT PRIMARY KEY, submitter_id TEXT NOT NULL REFERENCES users(id),
            entry_id TEXT NOT NULL REFERENCES public_entries(id), revision_id TEXT NOT NULL REFERENCES public_revisions(id),
            kind TEXT NOT NULL, detail TEXT NOT NULL, evidence_url TEXT,
            status TEXT NOT NULL, author_response TEXT, resolved_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_corrections_entry ON correction_suggestions(entry_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_corrections_submitter ON correction_suggestions(submitter_id,created_at DESC);
        CREATE TABLE IF NOT EXISTS public_profiles (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE REFERENCES users(id),
            display_name TEXT NOT NULL, bio TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS public_collections (
            id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
            curator_id TEXT NOT NULL REFERENCES users(id), published_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS public_collection_items (
            collection_id TEXT NOT NULL REFERENCES public_collections(id) ON DELETE CASCADE,
            entry_id TEXT NOT NULL REFERENCES public_entries(id) ON DELETE CASCADE,
            sort_order INTEGER NOT NULL DEFAULT 0, curator_note TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(collection_id,entry_id)
        );
        CREATE TABLE IF NOT EXISTS curation_records (
            id TEXT PRIMARY KEY, object_type TEXT NOT NULL, object_id TEXT NOT NULL,
            curator_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
            reason TEXT NOT NULL, sort_order INTEGER, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS public_search_documents (
            entry_id TEXT PRIMARY KEY REFERENCES public_entries(id) ON DELETE CASCADE,
            revision_id TEXT NOT NULL REFERENCES public_revisions(id), title TEXT NOT NULL,
            summary TEXT NOT NULL, body_text TEXT NOT NULL, public_category_id TEXT,
            category_name TEXT NOT NULL, public_tags TEXT NOT NULL, attribution TEXT NOT NULL,
            first_published_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_public_search_updated ON public_search_documents(status,updated_at DESC,entry_id);
        CREATE INDEX IF NOT EXISTS idx_public_search_latest ON public_search_documents(status,first_published_at DESC,entry_id);
        CREATE INDEX IF NOT EXISTS idx_public_search_category ON public_search_documents(status,public_category_id,updated_at DESC);
        CREATE TABLE IF NOT EXISTS public_index_jobs (
            entry_id TEXT PRIMARY KEY REFERENCES public_entries(id) ON DELETE CASCADE,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, not_before TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS public_search_meta (
            id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS public_revision_sources (
            revision_id TEXT NOT NULL REFERENCES public_revisions(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, label TEXT NOT NULL, url TEXT NOT NULL, kind TEXT NOT NULL,
            PRIMARY KEY(revision_id,position)
        );
        CREATE TABLE IF NOT EXISTS public_revision_reviews (
            revision_id TEXT PRIMARY KEY REFERENCES public_revisions(id) ON DELETE CASCADE,
            ai_policy_version TEXT, ai_model TEXT, ai_rules_version TEXT,
            ai_report_json TEXT, admin_reason TEXT NOT NULL, reviewed_by TEXT REFERENCES users(id), reviewed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS public_revision_taxonomy (
            revision_id TEXT PRIMARY KEY REFERENCES public_revisions(id) ON DELETE CASCADE,
            category_id TEXT REFERENCES public_categories(id), attribution TEXT NOT NULL,
            change_summary TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS public_revision_tags (
            revision_id TEXT NOT NULL REFERENCES public_revisions(id) ON DELETE CASCADE,
            tag_id TEXT NOT NULL REFERENCES public_tags(id),
            PRIMARY KEY(revision_id,tag_id)
        );
        CREATE TABLE IF NOT EXISTS submission_review_attempts (
            submission_id TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
            attempt INTEGER NOT NULL, status TEXT NOT NULL,
            policy_version TEXT, provider TEXT, model TEXT, rules_version TEXT,
            report_json TEXT, started_at TEXT NOT NULL, completed_at TEXT,
            PRIMARY KEY(submission_id,attempt)
        );
        CREATE INDEX IF NOT EXISTS idx_submission_review_attempts_status
        ON submission_review_attempts(status,started_at);
    """)
    for table, columns in {
        "public_entries": {
            "public_category_id": "TEXT REFERENCES public_categories(id)",
            "first_published_at": "TEXT",
            "public_profile_id": "TEXT REFERENCES public_profiles(id)",
            "featured_order": "INTEGER",
        },
        "public_revisions": {
            "visibility": "TEXT NOT NULL DEFAULT 'public'",
            "isolation_reason": "TEXT",
            "isolated_by": "TEXT REFERENCES users(id)",
            "isolated_at": "TEXT",
        },
        "submissions": {
            "proposed_public_category_id": "TEXT REFERENCES public_categories(id)",
            "proposed_tags_json": "TEXT",
            "reuse_permission": "TEXT NOT NULL DEFAULT 'view_only'",
            "link_public_profile": "INTEGER NOT NULL DEFAULT 0",
            "ai_policy_version": "TEXT",
            "ai_model": "TEXT",
            "ai_rules_version": "TEXT",
            "duplicate_candidates_json": "TEXT",
            "review_attempt": "INTEGER NOT NULL DEFAULT 0",
        },
        "reports": {
            "revision_id": "TEXT REFERENCES public_revisions(id)",
            "resolution_detail": "TEXT",
        },
        "public_imports": {
            "policy_version": "TEXT",
        },
        "public_index_jobs": {
            "not_before": "TEXT",
        },
    }.items():
        existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    try:
        db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS public_search_fts USING fts5(
                entry_id UNINDEXED,title,summary,body_text,category_name,public_tags,attribution,
                tokenize='unicode61 remove_diacritics 2'
            )
        """)
    except sqlite3.OperationalError as exc:
        raise RuntimeError("SQLite FTS5 is required for Square V2 search") from exc
    now = _now()
    rows = db.execute("SELECT id,snapshot_json FROM public_revisions").fetchall()
    for row in rows:
        try:
            category = str(json.loads(row["snapshot_json"]).get("category") or "").strip()
        except (TypeError, json.JSONDecodeError):
            continue
        if category:
            db.execute("""
                INSERT INTO public_category_mappings(private_label,status,updated_at)
                VALUES(?,'pending',?) ON CONFLICT(private_label) DO NOTHING
            """, (category, now))
    db.execute("""
        INSERT OR IGNORE INTO public_reuse_permissions(entry_id,permission,policy_version)
        SELECT id,'view_only',? FROM public_entries
    """, (REUSE_POLICY_VERSION,))
    db.execute(
        "UPDATE public_imports SET policy_version=? WHERE policy_version IS NULL OR policy_version=''",
        (REUSE_POLICY_VERSION,),
    )
    db.execute("INSERT OR IGNORE INTO public_search_meta(id,generation) VALUES(1,0)")


class SquareMixin:
    """Methods mixed into PlatformStore; the host supplies locking, audit, and auth helpers."""

    @staticmethod
    def _authorize_active_user_in_transaction(db: sqlite3.Connection, user_id: str) -> None:
        row = db.execute("SELECT status FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None or row["status"] != "active":
            raise FileNotFoundError(user_id)

    @staticmethod
    def _public_summary(row: sqlite3.Row) -> dict:
        tags = json.loads(row["tags_json"] or "[]") if "tags_json" in row.keys() else []
        return {
            "id": row["id"], "revision_id": row["revision_id"], "version": row["version"],
            "title": row["title"], "summary": row["summary"], "attribution": row["attribution"],
            "category": {"id": row["category_id"], "slug": row["category_slug"], "name": row["category_name"]},
            "tags": tags, "published_at": row["published_at"], "first_published_at": row["first_published_at"],
            "updated_at": row["search_updated_at"] if "search_updated_at" in row.keys() else row["published_at"],
            "source_count": row["source_count"] if "source_count" in row.keys() else 0,
            "content_hash": row["content_hash"], "featured": row["featured_order"] is not None,
        }

    @staticmethod
    def _safe_public_sources(snapshot: dict) -> list[dict]:
        return _safe_sources(snapshot)

    @staticmethod
    def _safe_source_count(db: sqlite3.Connection, revision_id: str) -> int:
        rows = db.execute(
            "SELECT label,url,kind FROM public_revision_sources WHERE revision_id=?", (revision_id,),
        ).fetchall()
        return len(_safe_sources({"public_sources": [dict(row) for row in rows]}))

    @staticmethod
    def _public_snapshot(snapshot: dict) -> dict:
        return _public_snapshot(snapshot)

    def _sync_square_entry(self, db: sqlite3.Connection, entry_id: str) -> None:
        row = db.execute("""
            SELECT e.id,e.status,e.current_revision_id,e.created_at,e.first_published_at,e.public_category_id,
                   r.snapshot_json,r.published_at,c.name category_name
            FROM public_entries e JOIN public_revisions r ON r.id=e.current_revision_id
            LEFT JOIN public_categories c ON c.id=e.public_category_id AND c.status='active' WHERE e.id=?
        """, (entry_id,)).fetchone()
        db.execute("DELETE FROM public_search_documents WHERE entry_id=?", (entry_id,))
        db.execute("DELETE FROM public_search_fts WHERE entry_id=?", (entry_id,))
        if row is None or row["status"] != "published":
            return
        snapshot = json.loads(row["snapshot_json"])
        tags = [item[0] for item in db.execute("""
            SELECT t.name FROM public_entry_tags et JOIN public_tags t ON t.id=et.tag_id
            WHERE et.entry_id=? AND t.status='active' ORDER BY t.name
        """, (entry_id,)).fetchall()]
        title = str(snapshot.get("title") or "")[:300]
        summary = str(snapshot.get("summary") or "")[:2000]
        body = re.sub(r"[`#>*_\[\]()]", " ", _square_public_markdown(str(snapshot.get("markdown") or "")))[:200_000]
        category_name = str(row["category_name"] or "待公共分类")[:120]
        attribution = str(snapshot.get("attribution") or "匿名用户")[:120]
        first = row["first_published_at"] or row["created_at"] or row["published_at"]
        db.execute("""
            INSERT INTO public_search_documents VALUES(?,?,?,?,?,?,?,?,?,?,?,'published')
        """, (entry_id, row["current_revision_id"], title, summary, body, row["public_category_id"],
              category_name, "; ".join(tags), attribution, first, row["published_at"]))
        db.execute("INSERT INTO public_search_fts VALUES(?,?,?,?,?,?,?)", (
            entry_id, _search_projection(title), _search_projection(summary), _search_projection(body),
            _search_projection(category_name), _search_projection(" ".join(tags)), _search_projection(attribution),
        ))

    @staticmethod
    def _bump_search_generation(db: sqlite3.Connection) -> None:
        db.execute("UPDATE public_search_meta SET generation=generation+1 WHERE id=1")

    def _refresh_square_entry(self, db: sqlite3.Connection, entry_id: str) -> bool:
        """Refresh a derived search projection without coupling it to the public fact write."""
        db.execute("SAVEPOINT square_index_refresh")
        try:
            self._sync_square_entry(db, entry_id)
            db.execute("DELETE FROM public_index_jobs WHERE entry_id=?", (entry_id,))
            self._bump_search_generation(db)
            db.execute("RELEASE square_index_refresh")
            return True
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            db.execute("ROLLBACK TO square_index_refresh")
            db.execute("RELEASE square_index_refresh")
            db.execute("""
                INSERT INTO public_index_jobs(entry_id,status,attempts,last_error,not_before,updated_at)
                VALUES(?,'pending',1,?,NULL,?)
                ON CONFLICT(entry_id) DO UPDATE SET status='pending',attempts=attempts+1,
                  last_error=excluded.last_error,not_before=NULL,updated_at=excluded.updated_at
            """, (entry_id, str(exc)[:500], _now()))
            self._bump_search_generation(db)
            return False

    def claim_public_index_job(self) -> dict | None:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="seconds")
        stale = now_dt - timedelta(minutes=5)

        def job_time(value: Any) -> datetime:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            return parsed.astimezone(timezone.utc)

        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for running in db.execute(
                "SELECT entry_id,updated_at FROM public_index_jobs WHERE status='running'",
            ):
                try:
                    updated_at = job_time(running["updated_at"])
                    is_stale = updated_at < stale
                except (OverflowError, TypeError, ValueError):
                    db.execute("""
                        UPDATE public_index_jobs
                        SET status='dead',last_error=?,not_before=NULL,updated_at=? WHERE entry_id=?
                    """, ("invalid index job state", now, running["entry_id"]))
                    continue
                if is_stale:
                    db.execute("""
                        UPDATE public_index_jobs SET status='retry',not_before=?,updated_at=? WHERE entry_id=?
                    """, (now, now, running["entry_id"]))
                elif str(running["updated_at"]) != updated_at.isoformat(timespec="seconds"):
                    db.execute(
                        "UPDATE public_index_jobs SET updated_at=? WHERE entry_id=?",
                        (updated_at.isoformat(timespec="seconds"), running["entry_id"]),
                    )
            row = None
            for candidate in db.execute("""
                SELECT entry_id,attempts,typeof(attempts) attempt_type,not_before,updated_at
                FROM public_index_jobs WHERE status IN ('pending','retry')
                ORDER BY updated_at,entry_id
            """):
                try:
                    attempts = int(candidate["attempts"])
                    if candidate["attempt_type"] != "integer" or attempts < 0:
                        raise ValueError
                    updated_at = job_time(candidate["updated_at"])
                    if candidate["not_before"] is not None:
                        not_before = job_time(candidate["not_before"])
                        if not_before > now_dt:
                            db.execute("""
                                UPDATE public_index_jobs SET not_before=?,updated_at=? WHERE entry_id=?
                            """, (
                                not_before.isoformat(timespec="seconds"),
                                updated_at.isoformat(timespec="seconds"),
                                candidate["entry_id"],
                            ))
                            continue
                except (OverflowError, TypeError, ValueError):
                    db.execute("""
                        UPDATE public_index_jobs
                        SET status='dead',attempts=0,last_error=?,not_before=NULL,updated_at=?
                        WHERE entry_id=?
                    """, ("invalid index job state", now, candidate["entry_id"]))
                    continue
                row = candidate
                break
            if row is None:
                db.commit(); return None
            attempt = int(row["attempts"]) + 1
            db.execute(
                "UPDATE public_index_jobs SET status='running',attempts=?,updated_at=? WHERE entry_id=?",
                (attempt, now, row["entry_id"]),
            )
            db.commit()
        return {"entry_id": row["entry_id"], "attempt": attempt}

    def process_public_index_job(self, entry_id: str, attempt: int) -> bool:
        try:
            with self._lock, self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT status,attempts FROM public_index_jobs WHERE entry_id=?", (entry_id,),
                ).fetchone()
                if current is None or current["status"] != "running" or int(current["attempts"]) != attempt:
                    db.rollback(); return False
                self._sync_square_entry(db, entry_id)
                self._bump_search_generation(db)
                db.execute("DELETE FROM public_index_jobs WHERE entry_id=? AND attempts=?", (entry_id, attempt))
                db.commit()
            return True
        except Exception as exc:
            status = "dead" if attempt >= 5 else "retry"
            next_time = None if status == "dead" else (
                datetime.now(timezone.utc) + timedelta(seconds=min(300, 2 ** attempt))
            ).isoformat(timespec="seconds")
            with self._lock, self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""
                    UPDATE public_index_jobs SET status=?,last_error=?,not_before=?,updated_at=?
                    WHERE entry_id=? AND status='running' AND attempts=?
                """, (status, str(exc)[:500], next_time, _now(), entry_id, attempt))
                db.commit()
            return False

    def retry_public_index_jobs(self, context: Any, entry_id: str | None = None) -> dict:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); self._authorize_admin_in_transaction(db, context.user_id)
            if entry_id:
                cursor = db.execute(
                    "UPDATE public_index_jobs SET status='pending',not_before=NULL,updated_at=? WHERE entry_id=?",
                    (_now(), entry_id),
                )
            else:
                cursor = db.execute(
                    "UPDATE public_index_jobs SET status='pending',not_before=NULL,updated_at=? WHERE status='dead'",
                    (_now(),),
                )
            count = cursor.rowcount
            db.commit()
        return {"queued": count}

    def rebuild_public_search(self) -> dict:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ids = [row[0] for row in db.execute("SELECT id FROM public_entries").fetchall()]
            indexed = sum(1 for entry_id in ids if self._refresh_square_entry(db, entry_id))
            pending = db.execute("SELECT COUNT(*) FROM public_index_jobs").fetchone()[0]
            db.commit()
        return {"indexed": indexed, "pending": pending}

    def public_categories(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT c.id,c.slug,c.name,c.description,c.sort_order,COUNT(e.id) entry_count
                FROM public_categories c LEFT JOIN public_search_documents d
                  ON d.public_category_id=c.id AND d.status='published'
                LEFT JOIN public_entries e ON e.id=d.entry_id AND e.status='published'
                  AND e.current_revision_id=d.revision_id
                LEFT JOIN public_revisions r ON r.id=e.current_revision_id AND r.visibility='public'
                WHERE c.status='active' AND (e.id IS NULL OR r.id IS NOT NULL)
                GROUP BY c.id ORDER BY c.sort_order,c.name
            """).fetchall()
        return [dict(row) for row in rows]

    def resolve_public_category(self, slug: str) -> dict:
        with self.connect() as db:
            row = db.execute("""
                SELECT c.id,c.slug,c.name,c.description,c.sort_order,
                       CASE WHEN c.slug=? THEN NULL ELSE ? END redirected_from
                FROM public_categories c
                LEFT JOIN public_category_slug_redirects redirect ON redirect.category_id=c.id
                WHERE c.status='active' AND (c.slug=? OR redirect.slug=?)
                ORDER BY CASE WHEN c.slug=? THEN 0 ELSE 1 END LIMIT 1
            """, (slug, slug, slug, slug, slug)).fetchone()
        if row is None:
            raise FileNotFoundError(slug)
        return dict(row)

    def public_tags(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT t.id,t.slug,t.name,COUNT(e.id) entry_count FROM public_tags t
                LEFT JOIN public_entry_tags et ON et.tag_id=t.id
                LEFT JOIN public_search_documents d ON d.entry_id=et.entry_id AND d.status='published'
                LEFT JOIN public_entries e ON e.id=d.entry_id AND e.status='published'
                  AND e.current_revision_id=d.revision_id
                LEFT JOIN public_revisions r ON r.id=e.current_revision_id AND r.visibility='public'
                WHERE t.status='active' AND (e.id IS NULL OR r.id IS NOT NULL)
                GROUP BY t.id ORDER BY entry_count DESC,t.name LIMIT 200
            """).fetchall()
        return [dict(row) for row in rows]

    def search_public(self, *, query: str = "", category: str = "", tag: str = "", sort: str = "updated",
                      cursor: str | None = None, limit: int = 24) -> dict:
        with self._lock:
            return self._search_public_locked(
                query=query, category=category, tag=tag, sort=sort, cursor=cursor, limit=limit,
            )

    def _search_public_locked(self, *, query: str = "", category: str = "", tag: str = "",
                              sort: str = "updated", cursor: str | None = None,
                              limit: int = 24) -> dict:
        query = query.strip()
        if len(query) > 120 or len(query.split()) > 8:
            raise ValueError("search query is too complex")
        if sort not in PUBLIC_SORTS or limit < 1 or limit > 50:
            raise ValueError("invalid public search options")
        with self.connect() as generation_db:
            generation = int(generation_db.execute(
                "SELECT generation FROM public_search_meta WHERE id=1",
            ).fetchone()[0])
        signature = hashlib.sha256(_json({
            "query": query.casefold(), "category": category, "tag": tag, "sort": sort, "limit": limit,
            "generation": generation,
        }).encode()).hexdigest()[:24]
        position = _decode_cursor(cursor, signature)
        where = [
            "d.status='published'", "e.status='published'", "r.visibility='public'",
            "e.current_revision_id=d.revision_id", "r.id=d.revision_id",
        ]
        params: list[Any] = []
        join_fts = ""
        rank = "0.0"
        if query:
            terms = _search_terms(query)
            if not terms:
                raise ValueError("search query has no searchable terms")
            fts_query = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
            join_fts = "JOIN public_search_fts f ON f.entry_id=d.entry_id"
            where.append("public_search_fts MATCH ?")
            params.append(fts_query)
            rank = "bm25(public_search_fts,8.0,4.0,2.0,3.0,5.0,1.0)"
        if category:
            category = self.resolve_public_category(category)["slug"]
            where.append("c.slug=?")
            params.append(category)
        if tag:
            where.append("EXISTS(SELECT 1 FROM public_entry_tags xt JOIN public_tags tt ON tt.id=xt.tag_id WHERE xt.entry_id=e.id AND tt.slug=? AND tt.status='active')")
            params.append(tag)
        after = ""
        if position is not None:
            if query and sort == "relevance":
                if len(position) != 3 or not isinstance(position[0], (int, float)) or not all(isinstance(value, str) for value in position[1:]):
                    raise ValueError("invalid cursor")
                after = "WHERE rank_value>? OR (rank_value=? AND (search_updated_at<? OR (search_updated_at=? AND id>?)))"
                params.extend([position[0], position[0], position[1], position[1], position[2]])
            else:
                if len(position) != 2 or not all(isinstance(value, str) for value in position):
                    raise ValueError("invalid cursor")
                key = "first_published_at" if sort == "latest" else "search_updated_at"
                after = f"WHERE {key}<? OR ({key}=? AND id>?)"
                params.extend([position[0], position[0], position[1]])
        outer_order = "rank_value ASC,search_updated_at DESC,id" if query and sort == "relevance" else (
            "first_published_at DESC,id" if sort == "latest" else "search_updated_at DESC,id"
        )
        params.append(limit + 1)
        with self.connect() as db:
            rows = db.execute(f"""
                WITH candidates AS (SELECT e.id,r.id revision_id,r.version,r.content_hash,r.published_at,
                       d.title,d.summary,d.attribution,d.first_published_at,e.featured_order,
                       d.updated_at search_updated_at,{rank} rank_value,
                       0 source_count,
                       c.id category_id,c.slug category_slug,COALESCE(c.name,d.category_name) category_name,
                       (SELECT json_group_array(json_object('id',t.id,'slug',t.slug,'name',t.name))
                        FROM public_entry_tags et JOIN public_tags t ON t.id=et.tag_id
                        WHERE et.entry_id=e.id AND t.status='active') tags_json
                FROM public_search_documents d {join_fts}
                JOIN public_entries e ON e.id=d.entry_id JOIN public_revisions r ON r.id=d.revision_id
                LEFT JOIN public_categories c ON c.id=e.public_category_id
                WHERE {' AND '.join(where)})
                SELECT * FROM candidates {after} ORDER BY {outer_order} LIMIT ?
            """, params).fetchall()
            item_rows = []
            for row in rows[:limit]:
                value = dict(row)
                value["source_count"] = self._safe_source_count(db, row["revision_id"])
                item_rows.append(value)
        items = [self._public_summary(row) for row in item_rows]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            position = ([last["rank_value"], last["search_updated_at"], last["id"]]
                        if query and sort == "relevance"
                        else [last["first_published_at"] if sort == "latest" else last["search_updated_at"], last["id"]])
            next_cursor = _cursor(position, signature)
        return {"items": items, "next_cursor": next_cursor}

    def _featured_public(self, limit: int = 8) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT e.id,r.id revision_id,r.version,r.content_hash,r.published_at,
                       d.title,d.summary,d.attribution,d.first_published_at,e.featured_order,
                       d.updated_at search_updated_at,
                       0 source_count,
                       c.id category_id,c.slug category_slug,COALESCE(c.name,d.category_name) category_name,
                       (SELECT json_group_array(json_object('id',t.id,'slug',t.slug,'name',t.name))
                        FROM public_entry_tags et JOIN public_tags t ON t.id=et.tag_id
                        WHERE et.entry_id=e.id AND t.status='active') tags_json
                FROM public_entries e
                JOIN public_search_documents d ON d.entry_id=e.id
                JOIN public_revisions r ON r.id=e.current_revision_id AND r.id=d.revision_id
                LEFT JOIN public_categories c ON c.id=e.public_category_id
                WHERE e.status='published' AND d.status='published' AND r.visibility='public'
                  AND e.featured_order IS NOT NULL
                ORDER BY e.featured_order,e.updated_at DESC,e.id LIMIT ?
            """, (min(max(limit, 1), 50),)).fetchall()
            values = []
            for row in rows:
                value = dict(row)
                value["source_count"] = self._safe_source_count(db, row["revision_id"])
                values.append(value)
        return [self._public_summary(row) for row in values]

    def public_home(self) -> dict:
        return {
            "categories": self.public_categories(), "tags": self.public_tags()[:20],
            "featured": self._featured_public(8),
            "latest": self.search_public(sort="latest", limit=8)["items"],
            "updated": self.search_public(sort="updated", limit=8)["items"],
            "collections": self.list_public_collections(limit=6),
        }

    def get_public_v2(
        self, entry_id: str, viewer_id: str | None = None,
        viewer_workspace_id: str | None = None,
    ) -> dict:
        with self.connect() as db:
            row = db.execute("""
                SELECT e.id,e.author_id,e.public_profile_id,e.first_published_at,e.featured_order,
                       r.id revision_id,r.version,r.snapshot_json,r.content_hash,r.published_at,
                       c.id category_id,c.slug category_slug,c.name category_name,
                       p.id profile_id,p.display_name profile_name,
                       rp.permission reuse_permission,rp.policy_version reuse_policy_version,
                       rr.ai_policy_version,rr.ai_model,rr.ai_rules_version,
                       rr.ai_report_json,rr.admin_reason,
                       (SELECT COUNT(*) FROM public_revision_sources prs WHERE prs.revision_id=r.id) source_count,
                       (SELECT COUNT(*) FROM correction_suggestions cs WHERE cs.entry_id=e.id) correction_count
                FROM public_entries e JOIN public_revisions r ON r.id=e.current_revision_id
                LEFT JOIN public_categories c ON c.id=e.public_category_id
                LEFT JOIN public_profiles p ON p.id=e.public_profile_id AND p.status='active'
                LEFT JOIN public_reuse_permissions rp ON rp.entry_id=e.id
                LEFT JOIN public_revision_reviews rr ON rr.revision_id=r.id
                WHERE e.id=? AND e.status='published' AND r.visibility='public'
            """, (entry_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(entry_id)
            tags = [dict(item) for item in db.execute("""
                SELECT t.id,t.slug,t.name FROM public_entry_tags et JOIN public_tags t ON t.id=et.tag_id
                WHERE et.entry_id=? AND t.status='active' ORDER BY t.name
            """, (entry_id,)).fetchall()]
            stored_sources = [dict(item) for item in db.execute("""
                SELECT label,url,kind FROM public_revision_sources WHERE revision_id=? ORDER BY position
            """, (row["revision_id"],)).fetchall()]
            sources = _safe_sources({"public_sources": stored_sources})
            subscribed = bool(viewer_id and db.execute(
                "SELECT 1 FROM public_subscriptions WHERE user_id=? AND public_entry_id=? AND status='active'",
                (viewer_id, entry_id),
            ).fetchone())
            imported = bool(viewer_id and viewer_workspace_id and db.execute(
                "SELECT 1 FROM public_imports WHERE workspace_id=? AND public_revision_id=? AND status='complete'",
                (viewer_workspace_id, row["revision_id"]),
            ).fetchone())
        snapshot = json.loads(row["snapshot_json"])
        ai_report = json.loads(row["ai_report_json"]) if row["ai_report_json"] else None
        return {
            "id": row["id"], "revision_id": row["revision_id"], "version": row["version"],
            "snapshot": _public_snapshot(snapshot), "attribution": snapshot.get("attribution") or "匿名用户",
            "published_at": row["published_at"], "first_published_at": row["first_published_at"],
            "content_hash": row["content_hash"],
            "category": {"id": row["category_id"], "slug": row["category_slug"], "name": row["category_name"] or "待公共分类"},
            "tags": tags, "sources": sources,
            "source_count": len(sources), "correction_count": row["correction_count"],
            "review": {"ai_policy_version": row["ai_policy_version"], "ai_model": row["ai_model"], "ai_rules_version": row["ai_rules_version"], "issues": _public_review_issues(ai_report), "admin_reason": row["admin_reason"]},
            "author_profile": {"id": row["profile_id"], "display_name": row["profile_name"]} if row["profile_id"] else None,
            "reuse_permission": row["reuse_permission"] or "view_only",
            "reuse_policy_version": row["reuse_policy_version"] or REUSE_POLICY_VERSION,
            "steward_label": snapshot.get("attribution") or "匿名发布者", "subscribed": subscribed,
            "imported": imported, "featured": row["featured_order"] is not None,
            "can_manage": bool(viewer_id and viewer_id == row["author_id"]),
            "related": self.related_public(entry_id), "references": self.public_references(entry_id),
        }

    def public_entry_tombstone(self, entry_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT status,moderation_reason FROM public_entries WHERE id=?", (entry_id,),
            ).fetchone()
        if row is None:
            return None
        if row["status"] == "withdrawn_by_author" and row["moderation_reason"] == "account_deleted":
            return {
                "code": "public_entry_account_deleted",
                "error": "该词条已因作者注销而撤回，正文和历史版本不再公开。",
            }
        return None

    def public_versions(self, entry_id: str) -> list[dict]:
        with self.connect() as db:
            if db.execute("SELECT 1 FROM public_entries WHERE id=? AND status='published'", (entry_id,)).fetchone() is None:
                raise FileNotFoundError(entry_id)
            rows = db.execute("""
                SELECT r.id,r.version,r.content_hash,r.published_at,
                       tx.attribution,tx.change_summary,rr.reviewed_at,
                       c.id category_id,c.slug category_slug,c.name category_name,
                       (SELECT json_group_array(json_object('id',t.id,'slug',t.slug,'name',t.name))
                        FROM public_revision_tags rt JOIN public_tags t ON t.id=rt.tag_id
                        WHERE rt.revision_id=r.id) tags_json
                FROM public_revisions r
                LEFT JOIN public_revision_taxonomy tx ON tx.revision_id=r.id
                LEFT JOIN public_categories c ON c.id=tx.category_id
                LEFT JOIN public_revision_reviews rr ON rr.revision_id=r.id
                WHERE r.entry_id=? AND r.visibility='public' ORDER BY r.version DESC
            """, (entry_id,)).fetchall()
        return [{
            **{key: row[key] for key in row.keys() if key != "tags_json"},
            "category": {"id": row["category_id"], "slug": row["category_slug"], "name": row["category_name"]},
            "tags": json.loads(row["tags_json"] or "[]"),
        } for row in rows]

    def get_public_version(self, entry_id: str, version: int) -> dict:
        with self.connect() as db:
            row = db.execute("""
                SELECT r.id,r.version,r.snapshot_json,r.content_hash,r.published_at FROM public_revisions r
                JOIN public_entries e ON e.id=r.entry_id
                WHERE e.id=? AND e.status='published' AND r.version=? AND r.visibility='public'
            """, (entry_id, version)).fetchone()
        if row is None:
            raise FileNotFoundError(entry_id)
        return {"id": row["id"], "version": row["version"], "snapshot": _public_snapshot(json.loads(row["snapshot_json"])),
                "content_hash": row["content_hash"], "published_at": row["published_at"]}

    def public_diff(self, entry_id: str, from_version: int, to_version: int) -> dict:
        if from_version < 1 or to_version < 1 or from_version == to_version:
            raise ValueError("only adjacent public versions can be compared")
        versions = sorted(item["version"] for item in self.public_versions(entry_id))
        before_index = versions.index(from_version) if from_version in versions else -1
        if before_index < 0 or before_index + 1 >= len(versions) or versions[before_index + 1] != to_version:
            raise ValueError("only adjacent public versions can be compared")
        before, after = self.get_public_version(entry_id, from_version), self.get_public_version(entry_id, to_version)
        diff = "".join(difflib.unified_diff(
            before["snapshot"]["markdown"].splitlines(True), after["snapshot"]["markdown"].splitlines(True),
            fromfile=f"v{from_version}", tofile=f"v{to_version}", n=3,
        ))
        return {"entry_id": entry_id, "from_version": from_version, "to_version": to_version, "diff": diff[:300_000]}

    def related_public(self, entry_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT e.id,d.title,d.summary,d.updated_at,
                       (CASE WHEN e.public_category_id=source.public_category_id THEN 4 ELSE 0 END +
                        (SELECT COUNT(*) FROM public_entry_tags a JOIN public_entry_tags b ON b.tag_id=a.tag_id
                         WHERE a.entry_id=? AND b.entry_id=e.id)) score
                FROM public_entries e JOIN public_search_documents d ON d.entry_id=e.id
                JOIN public_revisions r ON r.id=e.current_revision_id
                  AND r.id=d.revision_id AND r.visibility='public'
                JOIN public_entries source ON source.id=?
                WHERE e.id<>? AND e.status='published' AND d.status='published'
                ORDER BY score DESC,d.updated_at DESC LIMIT 6
            """, (entry_id, entry_id, entry_id)).fetchall()
        return [dict(row) for row in rows if row["score"] > 0]

    def public_references(self, entry_id: str) -> list[dict]:
        current = self.get_public_version(entry_id, self._current_public_version(entry_id))
        markdown = current["snapshot"].get("markdown", "")
        titles = {item.strip() for item in re.findall(r"\[([^\]]+)\]\([^)]*\)", markdown)}
        if not titles:
            return []
        with self.connect() as db:
            rows = db.execute("""
                SELECT d.entry_id id,d.title,d.summary FROM public_search_documents d
                JOIN public_entries e ON e.id=d.entry_id AND e.status='published'
                  AND e.current_revision_id=d.revision_id
                JOIN public_revisions r ON r.id=d.revision_id AND r.visibility='public'
                WHERE d.status='published' AND d.entry_id<>? AND d.title IN (%s) LIMIT 6
            """ % ",".join("?" for _ in titles), (entry_id, *sorted(titles))).fetchall()
        return [dict(row) for row in rows]

    def _current_public_version(self, entry_id: str) -> int:
        with self.connect() as db:
            row = db.execute("""
                SELECT r.version FROM public_entries e JOIN public_revisions r ON r.id=e.current_revision_id
                WHERE e.id=? AND e.status='published' AND r.visibility='public'
            """, (entry_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(entry_id)
        return int(row[0])

    def set_subscription(self, context: Any, entry_id: str, active: bool) -> dict:
        now = _now()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_active_user_in_transaction(db, context.user_id)
            if db.execute("SELECT 1 FROM public_entries WHERE id=? AND status='published'", (entry_id,)).fetchone() is None:
                db.rollback(); raise FileNotFoundError(entry_id)
            db.execute("""
                INSERT INTO public_subscriptions VALUES(?,?,?,?,?)
                ON CONFLICT(user_id,public_entry_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at
            """, (context.user_id, entry_id, "active" if active else "inactive", now, now))
            self._audit(db, context.user_id, "public.subscribe" if active else "public.unsubscribe", "public_entry", entry_id)
            db.commit()
        return {"entry_id": entry_id, "subscribed": active}

    def begin_public_import(
        self, context: Any, entry_id: str, revision_id: str, *,
        expected_workspace_id: str, expected_policy_version: str, acknowledged: bool,
    ) -> dict:
        if expected_workspace_id != context.workspace_id:
            raise ValueError("current workspace changed; confirm the import destination again")
        if expected_policy_version != REUSE_POLICY_VERSION or acknowledged is not True:
            raise ValueError("current reuse policy must be acknowledged")
        self.authorize_workspace(context.user_id, context.workspace_id, "wiki.write")
        now = _now()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_workspace_in_transaction(db, context.user_id, context.workspace_id, "wiki.write")
            existing = db.execute(
                "SELECT * FROM public_imports WHERE workspace_id=? AND public_revision_id=?",
                (context.workspace_id, revision_id),
            ).fetchone()
            if existing is not None and existing["status"] == "complete":
                db.commit()
                return {**dict(existing), "replay": True, "existing": True}
            row = db.execute("""
                SELECT e.id,r.id revision_id,r.snapshot_json,r.content_hash,p.permission,p.policy_version
                FROM public_entries e JOIN public_revisions r ON r.entry_id=e.id
                JOIN public_reuse_permissions p ON p.entry_id=e.id
                WHERE e.id=? AND e.status='published' AND e.current_revision_id=r.id
                  AND r.id=? AND r.visibility='public'
                  AND p.permission='allow_private_copy' AND p.revoked_at IS NULL
                  AND p.policy_version=?
            """, (entry_id, revision_id, expected_policy_version)).fetchone()
            if row is None:
                db.rollback(); raise FileNotFoundError(entry_id)
            if existing:
                db.commit()
                return {**dict(existing), "snapshot": _public_snapshot(json.loads(row["snapshot_json"])), "replay": True, "existing": False}
            import_id, article_id = uuid.uuid4().hex, uuid.uuid4().hex
            title = str(json.loads(row["snapshot_json"]).get("title") or "公开词条")
            stem = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", title, flags=re.UNICODE).strip("-")[:80] or "public-entry"
            path = f"_inbox/{stem}-{article_id[:8]}.md"
            db.execute("""
                INSERT INTO public_imports(
                    id,user_id,workspace_id,public_entry_id,public_revision_id,private_article_id,
                    private_path,status,imported_at,created_at,updated_at,policy_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """, (import_id, context.user_id, context.workspace_id, entry_id, revision_id, article_id, path,
                  "pending", None, now, now, row["policy_version"]))
            self._audit(db, context.user_id, "public.import_begin", "public_import", import_id, {"entry_id": entry_id, "revision_id": revision_id})
            db.commit()
        return {"id": import_id, "user_id": context.user_id, "workspace_id": context.workspace_id,
                "public_entry_id": entry_id, "public_revision_id": revision_id, "private_article_id": article_id,
                "private_path": path, "status": "pending", "policy_version": row["policy_version"],
                "snapshot": _public_snapshot(json.loads(row["snapshot_json"])), "replay": False, "existing": False}

    def finish_public_import(self, context: Any, import_id: str, subscribe: bool = True) -> dict:
        now = _now()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_workspace_in_transaction(db, context.user_id, context.workspace_id, "wiki.write")
            row = db.execute("SELECT * FROM public_imports WHERE id=? AND workspace_id=?", (
                import_id, context.workspace_id,
            )).fetchone()
            if row is None:
                db.rollback(); raise FileNotFoundError(import_id)
            if row["status"] == "complete":
                db.commit()
                return dict(row)
            eligible = db.execute("""
                SELECT 1 FROM public_entries e
                JOIN public_revisions r ON r.id=e.current_revision_id
                JOIN public_reuse_permissions p ON p.entry_id=e.id
                WHERE e.id=? AND e.status='published' AND r.id=? AND r.visibility='public'
                  AND p.permission='allow_private_copy' AND p.revoked_at IS NULL
                  AND p.policy_version=?
            """, (row["public_entry_id"], row["public_revision_id"], row["policy_version"])).fetchone()
            if eligible is None:
                db.rollback(); raise FileNotFoundError(import_id)
            db.execute("UPDATE public_imports SET status='complete',imported_at=COALESCE(imported_at,?),updated_at=? WHERE id=?", (now, now, import_id))
            if subscribe:
                db.execute("""
                    INSERT INTO public_subscriptions VALUES(?,?,?,?,?)
                    ON CONFLICT(user_id,public_entry_id) DO UPDATE SET status='active',updated_at=excluded.updated_at
                """, (context.user_id, row["public_entry_id"], "active", now, now))
            self._audit(db, context.user_id, "public.import_complete", "public_import", import_id)
            saved = db.execute("SELECT * FROM public_imports WHERE id=?", (import_id,)).fetchone()
            db.commit()
        return dict(saved)

    def abandon_public_import(self, context: Any, import_id: str) -> None:
        """Remove an unfinished reservation after its file operation was rolled back."""
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_workspace_in_transaction(db, context.user_id, context.workspace_id, "wiki.write")
            db.execute(
                "DELETE FROM public_imports WHERE id=? AND workspace_id=? AND status='pending'",
                (import_id, context.workspace_id),
            )
            db.commit()

    def remap_public_import_paths(self, workspace_id: str, path_map: dict[str, str]) -> None:
        if not path_map:
            return
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for old, new in path_map.items():
                db.execute("""
                    UPDATE public_imports SET private_path=?,updated_at=?
                    WHERE workspace_id=? AND private_path=? AND status='complete'
                """, (new, _now(), workspace_id, old))
            db.commit()

    def update_public_import_path(self, context: Any, import_id: str, article_id: str, path: str) -> dict:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_workspace_in_transaction(db, context.user_id, context.workspace_id, "wiki.read")
            row = db.execute("""
                SELECT id FROM public_imports WHERE id=? AND workspace_id=?
                  AND private_article_id=? AND status='complete'
            """, (import_id, context.workspace_id, article_id)).fetchone()
            if row is None:
                db.rollback(); raise FileNotFoundError(import_id)
            db.execute("UPDATE public_imports SET private_path=?,updated_at=? WHERE id=?", (path, _now(), import_id))
            saved = db.execute("SELECT * FROM public_imports WHERE id=?", (import_id,)).fetchone()
            db.commit()
        return dict(saved)

    def create_correction(self, context: Any, entry_id: str, kind: str, detail: str, evidence_url: str = "") -> dict:
        if kind not in CORRECTION_KINDS or not detail.strip() or len(detail) > 4000:
            raise ValueError("invalid correction")
        if evidence_url:
            evidence_url = canonical_public_url(evidence_url) or ""
            if not evidence_url:
                raise ValueError("invalid evidence URL")
        now, correction_id = _now(), uuid.uuid4().hex
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_active_user_in_transaction(db, context.user_id)
            row = db.execute("SELECT current_revision_id,author_id FROM public_entries WHERE id=? AND status='published'", (entry_id,)).fetchone()
            if row is None:
                db.rollback(); raise FileNotFoundError(entry_id)
            db.execute("INSERT INTO correction_suggestions VALUES(?,?,?,?,?,?,?,'open',NULL,NULL,?,?)", (
                correction_id, context.user_id, entry_id, row["current_revision_id"], kind, detail.strip(), evidence_url or None, now, now,
            ))
            self._notify(db, row["author_id"], "public_correction", "correction", correction_id, "公开词条收到纠错建议", "纠错只针对指定公开版本，不会自动修改正文。")
            self._audit(db, context.user_id, "correction.create", "correction", correction_id, {"entry_id": entry_id})
            db.commit()
        return {"id": correction_id, "entry_id": entry_id, "revision_id": row["current_revision_id"], "status": "open"}

    def list_my_corrections(self, context: Any) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("""
                SELECT id,entry_id,revision_id,kind,detail,evidence_url,status,author_response,
                       resolved_at,created_at,updated_at
                FROM correction_suggestions WHERE submitter_id=? ORDER BY created_at DESC
            """, (context.user_id,)).fetchall()]

    def my_square_library(self, context: Any) -> dict:
        with self.connect() as db:
            imports = [dict(row) for row in db.execute("""
                SELECT i.id,i.public_entry_id entry_id,i.public_revision_id revision_id,i.private_path,
                       i.status,i.imported_at,i.policy_version,w.display_name workspace_name,
                       COALESCE(d.title,'公开词条不可用') title,
                       CASE WHEN d.entry_id IS NULL THEN 0 ELSE 1 END source_available
                FROM public_imports i JOIN workspaces w ON w.id=i.workspace_id
                LEFT JOIN public_search_documents d ON d.entry_id=i.public_entry_id
                WHERE i.user_id=? ORDER BY i.created_at DESC
            """, (context.user_id,)).fetchall()]
            subscriptions = [dict(row) for row in db.execute("""
                SELECT s.public_entry_id entry_id,s.status,s.updated_at,
                       COALESCE(d.title,'公开词条不可用') title
                FROM public_subscriptions s
                LEFT JOIN public_search_documents d ON d.entry_id=s.public_entry_id
                WHERE s.user_id=? ORDER BY s.updated_at DESC
            """, (context.user_id,)).fetchall()]
            profile = db.execute("""
                SELECT id,display_name,bio,status FROM public_profiles WHERE user_id=?
            """, (context.user_id,)).fetchone()
        return {"imports": imports, "subscriptions": subscriptions, "profile": dict(profile) if profile else None}

    def list_entry_corrections(self, context: Any, entry_id: str) -> list[dict]:
        with self.connect() as db:
            entry = db.execute("SELECT author_id FROM public_entries WHERE id=?", (entry_id,)).fetchone()
            current_admin = db.execute(
                "SELECT 1 FROM users WHERE id=? AND status='active' AND role='admin'", (context.user_id,),
            ).fetchone()
            if entry is None or (entry["author_id"] != context.user_id and current_admin is None):
                raise FileNotFoundError(entry_id)
            return [dict(row) for row in db.execute("""
                SELECT id,entry_id,revision_id,kind,detail,evidence_url,status,author_response,
                       resolved_at,created_at,updated_at
                FROM correction_suggestions WHERE entry_id=? ORDER BY created_at DESC
            """, (entry_id,)).fetchall()]

    def decide_correction(self, context: Any, correction_id: str, status: str, response: str) -> dict:
        if status not in {"acknowledged", "accepted", "rejected", "resolved"} or not response.strip():
            raise ValueError("invalid correction decision")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_active_user_in_transaction(db, context.user_id)
            row = db.execute("""
                SELECT c.*,e.author_id FROM correction_suggestions c JOIN public_entries e ON e.id=c.entry_id
                WHERE c.id=?
            """, (correction_id,)).fetchone()
            current_admin = db.execute(
                "SELECT 1 FROM users WHERE id=? AND status='active' AND role='admin'", (context.user_id,),
            ).fetchone()
            if row is None or (row["author_id"] != context.user_id and current_admin is None):
                db.rollback(); raise FileNotFoundError(correction_id)
            now = _now()
            db.execute("UPDATE correction_suggestions SET status=?,author_response=?,resolved_at=?,updated_at=? WHERE id=?", (
                status, response.strip(), now if status in {"accepted", "rejected", "resolved"} else None, now, correction_id,
            ))
            self._notify(db, row["submitter_id"], "correction_decided", "correction", correction_id, "纠错建议已有处理结果", response.strip()[:500])
            self._audit(db, context.user_id, "correction.decide", "correction", correction_id, {"status": status})
            db.commit()
        return {"id": correction_id, "status": status, "author_response": response.strip()}

    def set_public_profile(self, context: Any, enabled: bool, display_name: str, bio: str) -> dict:
        if len(display_name.strip()) > 80 or len(bio) > 1000:
            raise ValueError("invalid public profile")
        now = _now()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_active_user_in_transaction(db, context.user_id)
            row = db.execute("SELECT * FROM public_profiles WHERE user_id=?", (context.user_id,)).fetchone()
            profile_id = row["id"] if row else uuid.uuid4().hex
            db.execute("""
                INSERT INTO public_profiles VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name,bio=excluded.bio,status=excluded.status,updated_at=excluded.updated_at
            """, (profile_id, context.user_id, display_name.strip() or context.nickname, bio.strip(), "active" if enabled else "disabled", now, now))
            if not enabled:
                db.execute("UPDATE public_entries SET public_profile_id=NULL WHERE author_id=?", (context.user_id,))
            self._audit(db, context.user_id, "public_profile.enable" if enabled else "public_profile.disable", "public_profile", profile_id)
            db.commit()
        return {"id": profile_id, "display_name": display_name.strip() or context.nickname, "bio": bio.strip(), "status": "active" if enabled else "disabled"}

    def get_public_profile(self, profile_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT id,display_name,bio FROM public_profiles WHERE id=? AND status='active'", (profile_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(profile_id)
            entries = db.execute("""
                SELECT e.id,d.title,d.summary,d.updated_at FROM public_entries e
                JOIN public_search_documents d ON d.entry_id=e.id
                JOIN public_revisions r ON r.id=e.current_revision_id
                  AND r.id=d.revision_id AND r.visibility='public'
                WHERE e.public_profile_id=? AND e.status='published' AND d.status='published'
                ORDER BY d.updated_at DESC LIMIT 50
            """, (profile_id,)).fetchall()
        return {**dict(row), "entries": [dict(item) for item in entries]}

    def public_profile_tombstone(self, profile_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT status FROM public_profiles WHERE id=?", (profile_id,)).fetchone()
        if row is not None and row["status"] == "disabled":
            return {"code": "public_profile_disabled", "error": "该作者主页已停用。"}
        return None

    def list_public_collections(self, limit: int = 24) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT c.id,c.slug,c.title,c.description,c.published_at,
                       COUNT(CASE WHEN e.status='published' AND d.status='published'
                         AND e.current_revision_id=d.revision_id AND r.visibility='public' THEN 1 END) entry_count
                FROM public_collections c LEFT JOIN public_collection_items i ON i.collection_id=c.id
                LEFT JOIN public_entries e ON e.id=i.entry_id
                LEFT JOIN public_search_documents d ON d.entry_id=e.id
                LEFT JOIN public_revisions r ON r.id=e.current_revision_id
                WHERE c.status='published'
                GROUP BY c.id ORDER BY c.published_at DESC LIMIT ?
            """, (min(max(limit, 1), 50),)).fetchall()
        return [dict(row) for row in rows]

    def get_public_collection(self, slug: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT id,slug,title,description,published_at FROM public_collections WHERE slug=? AND status='published'", (slug,)).fetchone()
            if row is None:
                raise FileNotFoundError(slug)
            items = db.execute("""
                SELECT e.id,d.title,d.summary,i.curator_note,i.sort_order FROM public_collection_items i
                JOIN public_entries e ON e.id=i.entry_id JOIN public_search_documents d ON d.entry_id=e.id
                JOIN public_revisions r ON r.id=e.current_revision_id
                  AND r.id=d.revision_id AND r.visibility='public'
                WHERE i.collection_id=? AND e.status='published' AND d.status='published'
                ORDER BY i.sort_order,e.id
            """, (row["id"],)).fetchall()
        return {**dict(row), "items": [dict(item) for item in items]}

    def admin_upsert_category(self, context: Any, category_id: str | None, slug: str, name: str, description: str, status: str, sort_order: int) -> dict:
        if status not in {"active", "disabled"} or not name.strip() or len(name) > 80:
            raise ValueError("invalid public category")
        category_id, now, requested_slug = category_id or uuid.uuid4().hex, _now(), _slug(slug)
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); self._authorize_admin_in_transaction(db, context.user_id)
            old = db.execute("SELECT slug FROM public_categories WHERE id=?", (category_id,)).fetchone()
            canonical_owner = db.execute(
                "SELECT id FROM public_categories WHERE slug=? AND id<>?", (requested_slug, category_id),
            ).fetchone()
            alias_owner = db.execute(
                "SELECT category_id FROM public_category_slug_redirects WHERE slug=?", (requested_slug,),
            ).fetchone()
            if canonical_owner is not None or (alias_owner is not None and alias_owner["category_id"] != category_id):
                db.rollback(); raise ValueError("public category slug is reserved")
            if alias_owner is not None:
                db.execute("DELETE FROM public_category_slug_redirects WHERE slug=? AND category_id=?", (requested_slug, category_id))
            db.execute("""
                INSERT INTO public_categories VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET slug=excluded.slug,name=excluded.name,description=excluded.description,status=excluded.status,sort_order=excluded.sort_order,updated_at=excluded.updated_at
            """, (category_id, requested_slug, name.strip(), description.strip()[:1000], status, int(sort_order), context.user_id, now, now))
            if old is not None and old["slug"] != requested_slug:
                existing_alias = db.execute(
                    "SELECT category_id FROM public_category_slug_redirects WHERE slug=?", (old["slug"],),
                ).fetchone()
                if existing_alias is not None and existing_alias["category_id"] != category_id:
                    db.rollback(); raise ValueError("public category slug is reserved")
                db.execute(
                    "INSERT OR IGNORE INTO public_category_slug_redirects VALUES(?,?,?)",
                    (old["slug"], category_id, now),
                )
            for entry in db.execute("SELECT id FROM public_entries WHERE public_category_id=?", (category_id,)).fetchall():
                self._refresh_square_entry(db, entry["id"])
            self._audit(db, context.user_id, "public_category.upsert", "public_category", category_id, {"status": status})
            db.commit()
        return {"id": category_id, "slug": requested_slug, "name": name.strip(), "description": description.strip(), "status": status, "sort_order": int(sort_order)}

    def admin_map_category(self, context: Any, private_label: str, category_id: str) -> dict:
        label = private_label.strip()
        if not label or len(label) > 120:
            raise ValueError("invalid private category label")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); self._authorize_admin_in_transaction(db, context.user_id)
            if db.execute("SELECT 1 FROM public_categories WHERE id=? AND status='active'", (category_id,)).fetchone() is None:
                db.rollback(); raise FileNotFoundError(category_id)
            now = _now()
            db.execute("""
                INSERT INTO public_category_mappings VALUES(?,?,'mapped',?,?)
                ON CONFLICT(private_label) DO UPDATE SET category_id=excluded.category_id,
                  status='mapped',mapped_by=excluded.mapped_by,updated_at=excluded.updated_at
            """, (label, category_id, context.user_id, now))
            migrated = 0
            rows = db.execute("""
                SELECT e.id,r.snapshot_json FROM public_entries e
                JOIN public_revisions r ON r.id=e.current_revision_id
                WHERE e.public_category_id IS NULL
            """).fetchall()
            for row in rows:
                try:
                    private_category = str(json.loads(row["snapshot_json"]).get("category") or "").strip()
                except (TypeError, json.JSONDecodeError):
                    continue
                if private_category != label:
                    continue
                db.execute("UPDATE public_entries SET public_category_id=? WHERE id=?", (category_id, row["id"]))
                self._refresh_square_entry(db, row["id"])
                migrated += 1
            self._audit(db, context.user_id, "public_category.map", "private_category", label, {"category_id": category_id})
            db.commit()
        return {"private_label": label, "category_id": category_id, "status": "mapped", "migrated": migrated}

    def admin_merge_category(self, context: Any, source_id: str, target_id: str, reason: str) -> dict:
        if source_id == target_id or not reason.strip():
            raise ValueError("a distinct target and reason are required")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); self._authorize_admin_in_transaction(db, context.user_id)
            source = db.execute("SELECT id,slug FROM public_categories WHERE id=? AND status<>'merged'", (source_id,)).fetchone()
            target = db.execute("SELECT id,slug FROM public_categories WHERE id=? AND status='active'", (target_id,)).fetchone()
            if source is None or target is None:
                db.rollback(); raise FileNotFoundError(source_id if source is None else target_id)
            now = _now()
            db.execute("UPDATE public_entries SET public_category_id=? WHERE public_category_id=?", (target_id, source_id))
            db.execute("UPDATE public_category_mappings SET category_id=?,status='mapped',mapped_by=?,updated_at=? WHERE category_id=?",
                       (target_id, context.user_id, now, source_id))
            db.execute("UPDATE public_categories SET status='merged',updated_at=? WHERE id=?", (now, source_id))
            db.execute("UPDATE public_category_slug_redirects SET category_id=? WHERE category_id=?", (target_id, source_id))
            conflicting = db.execute(
                "SELECT category_id FROM public_category_slug_redirects WHERE slug=?", (source["slug"],),
            ).fetchone()
            if conflicting is not None and conflicting["category_id"] not in {source_id, target_id}:
                db.rollback(); raise ValueError("public category slug is reserved")
            db.execute("INSERT OR REPLACE INTO public_category_slug_redirects VALUES(?,?,?)", (source["slug"], target_id, now))
            for row in db.execute("SELECT id FROM public_entries WHERE public_category_id=?", (target_id,)).fetchall():
                self._refresh_square_entry(db, row["id"])
            self._audit(db, context.user_id, "public_category.merge", "public_category", source_id,
                        {"target_id": target_id, "reason": reason.strip()})
            db.commit()
        return {"id": source_id, "status": "merged", "merged_into": target_id, "canonical_slug": target["slug"]}

    def admin_square_state(self, context: Any) -> dict:
        if context.role != "admin":
            raise PermissionError("admin role required")
        with self.connect() as db:
            self._authorize_admin_in_transaction(db, context.user_id)
            categories = [dict(row) for row in db.execute("SELECT * FROM public_categories ORDER BY sort_order,name").fetchall()]
            tags = [dict(row) for row in db.execute("SELECT * FROM public_tags ORDER BY name").fetchall()]
            collections = [dict(row) for row in db.execute("SELECT * FROM public_collections ORDER BY updated_at DESC").fetchall()]
            mappings = [dict(row) for row in db.execute("SELECT * FROM public_category_mappings ORDER BY status,private_label").fetchall()]
            corrections = [dict(row) for row in db.execute("""
                SELECT correction_suggestions.*,COALESCE(public_search_documents.title,'公开词条不可用') entry_title
                FROM correction_suggestions
                LEFT JOIN public_search_documents ON public_search_documents.entry_id=correction_suggestions.entry_id
                WHERE correction_suggestions.status IN ('open','acknowledged')
                ORDER BY correction_suggestions.created_at
            """).fetchall()]
            index_jobs = [dict(row) for row in db.execute(
                "SELECT entry_id,status,attempts,last_error,not_before,updated_at FROM public_index_jobs ORDER BY updated_at",
            ).fetchall()]
        return {"categories": categories, "tags": tags, "collections": collections,
                "category_mappings": mappings, "corrections": corrections, "index_jobs": index_jobs}

    def admin_public_versions(self, context: Any, entry_id: str) -> list[dict]:
        if context.role != "admin":
            raise PermissionError("admin role required")
        with self.connect() as db:
            self._authorize_admin_in_transaction(db, context.user_id)
            rows = db.execute("""
                SELECT id,version,snapshot_json,content_hash,published_at,visibility,isolation_reason
                FROM public_revisions WHERE entry_id=? ORDER BY version DESC
            """, (entry_id,)).fetchall()
            if not rows:
                raise FileNotFoundError(entry_id)
        return [{
            "id": row["id"], "version": row["version"],
            "snapshot": _public_snapshot(json.loads(row["snapshot_json"])),
            "content_hash": row["content_hash"], "published_at": row["published_at"],
            "visibility": row["visibility"], "isolation_reason": row["isolation_reason"],
        } for row in rows]

    def admin_upsert_tag(self, context: Any, tag_id: str | None, slug: str, name: str, status: str) -> dict:
        if status not in {"active", "disabled"} or not name.strip() or len(name) > 50:
            raise ValueError("invalid public tag")
        tag_id, now = tag_id or uuid.uuid4().hex, _now()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); self._authorize_admin_in_transaction(db, context.user_id)
            db.execute("""
                INSERT INTO public_tags VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET slug=excluded.slug,name=excluded.name,status=excluded.status,updated_at=excluded.updated_at
            """, (tag_id, _slug(slug), name.strip(), status, now, now))
            for entry in db.execute("SELECT entry_id FROM public_entry_tags WHERE tag_id=?", (tag_id,)).fetchall():
                self._refresh_square_entry(db, entry["entry_id"])
            self._audit(db, context.user_id, "public_tag.upsert", "public_tag", tag_id, {"status": status})
            db.commit()
        return {"id": tag_id, "slug": _slug(slug), "name": name.strip(), "status": status}

    def admin_set_entry_taxonomy(self, context: Any, entry_id: str, category_id: str | None, tag_ids: list[str]) -> dict:
        if len(tag_ids) > 12 or any(not re.fullmatch(r"[a-f0-9]{32}", value) for value in tag_ids):
            raise ValueError("invalid public tags")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); self._authorize_admin_in_transaction(db, context.user_id)
            if category_id and db.execute("SELECT 1 FROM public_categories WHERE id=? AND status='active'", (category_id,)).fetchone() is None:
                db.rollback(); raise FileNotFoundError(category_id)
            if db.execute("SELECT 1 FROM public_entries WHERE id=?", (entry_id,)).fetchone() is None:
                db.rollback(); raise FileNotFoundError(entry_id)
            db.execute("UPDATE public_entries SET public_category_id=? WHERE id=?", (category_id, entry_id))
            db.execute("DELETE FROM public_entry_tags WHERE entry_id=?", (entry_id,))
            for tag_id in dict.fromkeys(tag_ids):
                if db.execute("SELECT 1 FROM public_tags WHERE id=? AND status='active'", (tag_id,)).fetchone() is None:
                    db.rollback(); raise FileNotFoundError(tag_id)
                db.execute("INSERT INTO public_entry_tags VALUES(?,?)", (entry_id, tag_id))
            self._refresh_square_entry(db, entry_id)
            self._audit(db, context.user_id, "public_entry.taxonomy", "public_entry", entry_id, {"category_id": category_id, "tag_ids": tag_ids})
            db.commit()
        return {"id": entry_id, "public_category_id": category_id, "tag_ids": tag_ids}

    def admin_set_featured(self, context: Any, entry_id: str, featured: bool, reason: str, sort_order: int = 0) -> dict:
        if not reason.strip():
            raise ValueError("curation reason is required")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); self._authorize_admin_in_transaction(db, context.user_id)
            changed = db.execute("UPDATE public_entries SET featured_order=? WHERE id=? AND status='published'", (int(sort_order) if featured else None, entry_id)).rowcount
            if not changed:
                db.rollback(); raise FileNotFoundError(entry_id)
            db.execute("INSERT INTO curation_records VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, "public_entry", entry_id, context.user_id, "feature" if featured else "unfeature", reason.strip(), int(sort_order), _now()))
            self._audit(db, context.user_id, "public.feature" if featured else "public.unfeature", "public_entry", entry_id, {"reason": reason.strip(), "sort_order": sort_order})
            db.commit()
        return {"id": entry_id, "featured": featured, "sort_order": sort_order if featured else None}

    def admin_upsert_collection(self, context: Any, collection_id: str | None, slug: str, title: str, description: str, status: str, items: list[dict], reason: str) -> dict:
        if status not in {"draft", "published", "disabled"} or not title.strip() or not reason.strip() or len(items) > 100:
            raise ValueError("invalid public collection")
        collection_id, now = collection_id or uuid.uuid4().hex, _now()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); self._authorize_admin_in_transaction(db, context.user_id)
            db.execute("""
                INSERT INTO public_collections VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET slug=excluded.slug,title=excluded.title,description=excluded.description,status=excluded.status,published_at=excluded.published_at,updated_at=excluded.updated_at
            """, (collection_id, _slug(slug), title.strip(), description.strip()[:2000], status, context.user_id, now if status == "published" else None, now, now))
            db.execute("DELETE FROM public_collection_items WHERE collection_id=?", (collection_id,))
            for index, item in enumerate(items):
                entry_id = str(item.get("entry_id") or "")
                if db.execute("SELECT 1 FROM public_entries WHERE id=? AND status='published'", (entry_id,)).fetchone() is None:
                    db.rollback(); raise FileNotFoundError(entry_id)
                db.execute("INSERT INTO public_collection_items VALUES(?,?,?,?)", (collection_id, entry_id, index, str(item.get("note") or "")[:300]))
            db.execute("INSERT INTO curation_records VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, "public_collection", collection_id, context.user_id, "upsert", reason.strip(), None, now))
            self._audit(db, context.user_id, "public_collection.upsert", "public_collection", collection_id, {"status": status, "reason": reason.strip()})
            db.commit()
        return {"id": collection_id, "slug": _slug(slug), "title": title.strip(), "status": status}

    def admin_isolate_revision(self, context: Any, entry_id: str, revision_id: str, isolate: bool, reason: str) -> dict:
        if not reason.strip():
            raise ValueError("isolation reason is required")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); self._authorize_admin_in_transaction(db, context.user_id)
            row = db.execute("SELECT e.current_revision_id,r.visibility FROM public_entries e JOIN public_revisions r ON r.entry_id=e.id WHERE e.id=? AND r.id=?", (entry_id, revision_id)).fetchone()
            if row is None:
                db.rollback(); raise FileNotFoundError(revision_id)
            if isolate and row["current_revision_id"] == revision_id:
                db.rollback(); raise ValueError("take down the public entry instead of isolating its current revision")
            db.execute("UPDATE public_revisions SET visibility=?,isolation_reason=?,isolated_by=?,isolated_at=? WHERE id=?", (
                "isolated" if isolate else "public", reason.strip() if isolate else None, context.user_id, _now(), revision_id,
            ))
            self._audit(db, context.user_id, "public_revision.isolate" if isolate else "public_revision.restore", "public_revision", revision_id, {"reason": reason.strip()})
            db.commit()
        return {"id": revision_id, "visibility": "isolated" if isolate else "public"}

    def author_withdraw_public(self, context: Any, entry_id: str, reason: str) -> dict:
        if not reason.strip():
            raise ValueError("withdrawal reason is required")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); self._authorize_active_user_in_transaction(db, context.user_id)
            row = db.execute("""
                SELECT e.status,e.current_revision_id,r.snapshot_json FROM public_entries e
                JOIN public_revisions r ON r.id=e.current_revision_id
                WHERE e.id=? AND e.author_id=? AND e.status IN ('published','removed_by_admin')
            """, (entry_id, context.user_id)).fetchone()
            if row is None:
                db.rollback(); raise FileNotFoundError(entry_id)
            db.execute("UPDATE public_entries SET status='withdrawn_by_author',updated_at=?,moderation_reason=? WHERE id=?", (_now(), reason.strip(), entry_id))
            title = str(json.loads(row["snapshot_json"]).get("title") or "公开词条")
            if row["status"] == "published":
                for subscriber in db.execute("SELECT user_id FROM public_subscriptions WHERE public_entry_id=? AND status='active'", (entry_id,)).fetchall():
                    if subscriber["user_id"] != context.user_id:
                        self._notify(db, subscriber["user_id"], "public_withdrawn", "public_entry", entry_id,
                                     f"《{title}》已由作者撤回", "所有公开版本现已不可访问；你的私人副本不会被删除。")
            self._refresh_square_entry(db, entry_id)
            self._audit(db, context.user_id, "public.withdraw", "public_entry", entry_id, {"reason": reason.strip(), "revision_id": row["current_revision_id"]})
            db.commit()
        return {"id": entry_id, "status": "withdrawn_by_author"}

    def set_reuse_permission(
        self, context: Any, entry_id: str, permission: str, *,
        policy_version: str | None = None, acknowledged: bool = False,
    ) -> dict:
        if permission not in REUSE_PERMISSIONS:
            raise ValueError("invalid reuse permission")
        if permission == "allow_private_copy" and (
            policy_version != REUSE_POLICY_VERSION or acknowledged is not True
        ):
            raise ValueError("current reuse policy must be acknowledged")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize_active_user_in_transaction(db, context.user_id)
            row = db.execute("SELECT author_id FROM public_entries WHERE id=?", (entry_id,)).fetchone()
            if row is None or row["author_id"] != context.user_id:
                db.rollback(); raise FileNotFoundError(entry_id)
            now = _now()
            db.execute("""
                INSERT INTO public_reuse_permissions VALUES(?,?,?,?,?,?)
                ON CONFLICT(entry_id) DO UPDATE SET permission=excluded.permission,granted_by=excluded.granted_by,
                  granted_at=excluded.granted_at,revoked_at=excluded.revoked_at,policy_version=excluded.policy_version
            """, (entry_id, permission, context.user_id, now if permission == "allow_private_copy" else None,
                  now if permission == "view_only" else None, REUSE_POLICY_VERSION))
            self._audit(db, context.user_id, "public.reuse_permission", "public_entry", entry_id, {"permission": permission, "policy_version": REUSE_POLICY_VERSION})
            db.commit()
        return {"entry_id": entry_id, "permission": permission, "policy_version": REUSE_POLICY_VERSION}

    def duplicate_candidates(self, snapshot: dict, limit: int = 8) -> list[dict]:
        query = str(snapshot.get("title") or "").strip()
        if not query:
            return []
        try:
            return self.search_public(query=query, sort="relevance", limit=min(limit, 8))["items"]
        except (ValueError, sqlite3.OperationalError):
            return []


class PublicIndexWorker:
    def __init__(self, store: Any):
        self.store = store
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="public-index-worker", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.store.claim_public_index_job()
            except (sqlite3.Error, TypeError, ValueError):
                self._wake.wait(1)
                self._wake.clear()
                continue
            if job is None:
                self._wake.wait(1)
                self._wake.clear()
                continue
            try:
                self.store.process_public_index_job(job["entry_id"], job["attempt"])
            except Exception:
                self._wake.wait(1)
                self._wake.clear()
