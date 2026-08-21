"""Application service for reading, governing, and generating the Markdown wiki."""

from __future__ import annotations

import hashlib
import contextlib
import functools
import json
import os
import re
import threading
import time
import uuid
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Callable

import aliases
import categories as cats
import dynamic_categories as dc
import keywords as kw
import websearch
import wiki_ops
from document_ingest import MAX_INPUT_BYTES, SUPPORTED_SUFFIXES, parse_document_cached
from security import RemoteError, resolve_relative_file
from state_store import StateStore
from storage import FileStore, OperationExistsError, TransactionTargetExistsError

STATUS_VALUES = {"词条", "草稿", "过时", "有争议"}
META_LINE_RE = re.compile(r"^>\s*([A-Za-z]+):\s*(.*?)\s*$", re.M)
MD_LINK_RE = wiki_ops.MD_LINK_RE
MANAGED_BODY_META_RE = re.compile(
    r"^>\s*(?:Article-ID|Category-ID|Classification|Classification-Updated|Category|Status|Tags|Redirect):",
    re.M | re.I,
)


def contains_h1(markdown: str) -> bool:
    lines = markdown.splitlines()
    fenced = False
    fence_marker = ""
    fence_length = 0
    for index, line in enumerate(lines):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            marker_run = fence.group(1)
            marker = marker_run[0]
            remainder = fence.group(2)
            if fenced:
                if marker == fence_marker and len(marker_run) >= fence_length and not remainder.strip():
                    fenced = False
                    fence_marker = ""
                    fence_length = 0
                continue
            if marker == "`" and "`" in remainder:
                pass
            else:
                fenced = True
                fence_marker = marker
                fence_length = len(marker_run)
                continue
        if fenced:
            continue
        if re.match(r"^ {0,3}#(?:[ \t]+|$)", line):
            return True
        if index + 1 < len(lines) and line.strip() and re.match(r"^ {0,3}=+[ \t]*$", lines[index + 1]):
            return True
    return False


def slugify(term: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|]+", "", term.strip())
    text = re.sub(r"\s+", "-", text).strip("-.") or "keyword"
    return text[:60]


def revision(md: str) -> str:
    return hashlib.sha256(md.encode("utf-8")).hexdigest()


def meta_values(md: str, key: str) -> list[str]:
    match = re.search(rf"^>\s*{re.escape(key)}:\s*(.*?)\s*$", md, re.M | re.I)
    if not match:
        return []
    value = match.group(1).strip()
    if not value or value.startswith("（"):
        return []
    links = MD_LINK_RE.findall(value)
    if links:
        return [href for _label, href in links]
    return [part.strip() for part in re.split(r"[;；]", value) if part.strip()]


def replace_meta(md: str, key: str, value: str | None) -> str:
    pattern = re.compile(rf"^>\s*{re.escape(key)}:.*(?:\n|$)", re.M | re.I)
    if value is None or value == "":
        return pattern.sub("", md, count=1)
    line = f"> {key}: {value}\n"
    if pattern.search(md):
        return pattern.sub(line, md, count=1)
    lines = md.splitlines(keepends=True)
    insert_at = 1 if lines and lines[0].startswith("# ") else 0
    while insert_at < len(lines) and (not lines[insert_at].strip() or lines[insert_at].startswith(">")):
        insert_at += 1
    lines.insert(insert_at, line)
    return "".join(lines)


def article_summary(md: str) -> str:
    for paragraph in re.split(r"\n\s*\n", md):
        text = re.sub(r"^>.*$", "", paragraph, flags=re.M).strip()
        if text and not text.startswith("#"):
            return re.sub(r"\s+", " ", text)[:120]
    return ""


def extract_markdown_article(value: str, fallback_title: str | None = None) -> str:
    """Extract a complete Markdown article while tolerating model preambles/fences."""
    text = (value or "").strip()
    fenced = re.search(r"```(?:markdown|md)?\s*\n(.*?)```", text, re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("# "):
        heading = re.search(r"^#\s+\S.*$", text, re.M)
        if heading:
            text = text[heading.start():].strip()
    if not text.startswith("# ") and fallback_title and re.search(r"^##\s+\S.*$", text, re.M):
        text = f"# {fallback_title}\n\n{text}"
    if not text.startswith("# "):
        raise RemoteError("model_error", "The model did not return a Markdown article.")
    return text.rstrip() + "\n"


def model_message_text(message: object) -> str:
    """Normalize string and multipart OpenAI-compatible message content."""
    content = getattr(message, "content", None)
    if isinstance(content, str) and content.strip():
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            value = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
            if isinstance(value, str):
                parts.append(value)
    if parts:
        return "\n".join(parts)
    reasoning = getattr(message, "reasoning_content", None)
    if isinstance(reasoning, str) and re.search(r"^#\s+\S.*$", reasoning, re.M):
        return reasoning
    return ""


def source_link(from_rel: str, to_rel: str, title: str) -> str:
    return f"[{title}]({wiki_ops.rel_href(from_rel, to_rel)})"


def render_index(project_root: Path, overrides: dict[str, str | None] | None = None) -> str:
    overrides = overrides or {}
    registry_override = overrides.get(dc.REGISTRY_REL)
    registry = json.loads(registry_override) if isinstance(registry_override, str) else dc.load_registry(project_root)
    categories = sorted(registry["categories"], key=lambda item: (item["sort_order"], item["name"].casefold()))
    grouped: dict[str, list[tuple[str, str, str]]] = {item["category_id"]: [] for item in categories}
    rels = {kw.rel_article(project_root, path) for path in kw.iter_articles(project_root)}
    rels.update(rel.removeprefix("wiki/") for rel, value in overrides.items() if rel.startswith("wiki/") and value is not None and rel not in {"wiki/index.md", "wiki/log.md"})
    for rel in sorted(rels):
        wiki_rel = rel.removeprefix("wiki/")
        project_rel = f"wiki/{wiki_rel}"
        if project_rel in overrides:
            md = overrides[project_rel]
        else:
            path = project_root / "wiki" / wiki_rel
            if not path.is_file():
                continue
            md = path.read_text(encoding="utf-8")
        if not md or aliases.REDIRECT_RE.search(md[:4096]):
            continue
        title = kw.parse_title(md) or Path(wiki_rel).stem
        category_id = dc.meta_value(md, "Category-ID") or "_inbox"
        grouped.setdefault(category_id, []).append((wiki_rel, title, article_summary(md)))
    lines = ["# Knowledge Base Index", ""]
    display_categories = [*categories, {"category_id": "_inbox", "name": "未分类", "description": "尚未选择主分类的词条"}]
    for category in display_categories:
        rows = grouped.get(category["category_id"], [])
        if not rows:
            continue
        lines.extend([f"## {category['name']}", "", category.get("description", ""), "", "| Article | Summary | Updated |", "|---------|---------|---------|"])
        for rel, title, summary in sorted(rows, key=lambda item: item[1].casefold()):
            lines.append(f"| [{title}]({rel}) | {summary} | {date.today().isoformat()} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def append_log_text(current: str, *, operation_id: str, kind: str, title: str, result: str = "committed") -> str:
    base = current if current.strip() else "# Wiki Log\n"
    return (
        base.rstrip()
        + f"\n\n## [{date.today().isoformat()}] {kind} | {title}\n"
        + f"- Operation: {operation_id}\n- Result: {result}\n"
    )


def serialized_wiki_write(method):
    """Hold the cross-instance file lock across shared read-plan-commit writes."""
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._intent_lock:
            with self.files.locked():
                return method(self, *args, **kwargs)
    return wrapped


@dataclass
class LLMConfig:
    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    allow_private: bool = False
    allow_insecure_http: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)


REMOTE_TASK_KINDS = {"generate", "supplement", "governance"}


class RemoteTaskUnavailable(RuntimeError):
    def __init__(self, kind: str, reason: str):
        super().__init__("remote worker is unavailable")
        self.kind = kind
        self.reason = reason


class WikiService:
    def __init__(
        self,
        project_root: Path,
        *,
        llm_config: LLMConfig | None = None,
        remote_search: Callable[..., list[dict]] | None = None,
        start_worker: bool = True,
        remote_tasks_enabled: bool = True,
        remote_task_kinds: set[str] | None = None,
        authorize_actor: Callable[[str], bool] | None = None,
        actor_guard: Callable[[str], object] | None = None,
        require_task_actor: bool = False,
        path_remap_callback: Callable[[list[dict[str, str]]], None] | None = None,
    ):
        self.root = project_root.resolve()
        (self.root / "wiki").mkdir(exist_ok=True)
        (self.root / "raw").mkdir(exist_ok=True)
        self.files = FileStore(self.root)
        self.state = StateStore(self.root)
        self.path_remap_callback = path_remap_callback
        self._recover_path_projections()
        self._ensure_dynamic_registry()
        self.llm = llm_config or LLMConfig()
        self.remote_search = remote_search or websearch.search_sources
        self.remote_tasks_enabled = remote_tasks_enabled
        self.remote_task_kinds = REMOTE_TASK_KINDS.copy() if remote_task_kinds is None else set(remote_task_kinds)
        self.authorize_actor = authorize_actor
        self.actor_guard = actor_guard
        self.require_task_actor = require_task_actor
        self._request_context = threading.local()
        self._stop = threading.Event()
        self._intent_lock = threading.RLock()
        self._retired = False
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        if start_worker:
            self._worker = threading.Thread(target=self._worker_loop, name="wiki-remote-worker", daemon=True)
            self._worker.start()

    def close(self) -> None:
        with self._intent_lock:
            self._retired = True
            self._stop.set()
            self._wake.set()
        if self._worker:
            self._worker.join(timeout=3)

    @contextlib.contextmanager
    def intent_guard(self):
        with self._intent_lock:
            yield

    @property
    def retired(self) -> bool:
        return self._retired

    def reconcile_workspace_status(self, status: str) -> list[dict]:
        with self._intent_lock:
            if status == "active":
                tasks = self.state.resume_paused_tasks()
                if tasks:
                    self._wake.set()
                return tasks
            self._retired = True
            if status == "suspended":
                return self.state.pause_active_tasks()
            if status == "deleted":
                return self.state.terminate_workspace_tasks()
            raise ValueError("invalid workspace status")

    def pause_for_workspace_suspend(self) -> list[dict]:
        with self._intent_lock:
            return self.state.pause_active_tasks()

    def resume_after_workspace_restore(self) -> list[dict]:
        with self._intent_lock:
            tasks = self.state.resume_paused_tasks()
            if tasks:
                self._wake.set()
            return tasks

    def terminate_for_workspace_delete(self) -> list[dict]:
        with self._intent_lock:
            return self.state.terminate_workspace_tasks()

    def configure_llm(self, config: LLMConfig) -> None:
        with self._intent_lock:
            self.llm = config
        self._wake.set()

    def remote_task_availability(self, kind: str) -> dict:
        reason = None
        if not self.remote_tasks_enabled:
            reason = "disabled"
        elif kind not in self.remote_task_kinds:
            reason = "kind_disabled"
        return {"kind": kind, "available": reason is None, "reason": reason}

    def require_remote_task(self, kind: str) -> None:
        availability = self.remote_task_availability(kind)
        if not availability["available"]:
            raise RemoteTaskUnavailable(kind, str(availability["reason"]))

    def generation_task_requirement(self, preflight: dict) -> dict:
        if preflight.get("existing_path"):
            return {"required": False, "kind": None, "available": True, "reason": None}
        needs_remote = not preflight["local_coverage"]["sufficient"]
        required = needs_remote or self.llm.configured
        kind = "supplement" if needs_remote else "generate"
        availability = self.remote_task_availability(kind)
        return {
            "required": required,
            "kind": kind if required else None,
            "available": not required or availability["available"],
            "reason": availability["reason"] if required else None,
        }

    def require_generation_task(self, preflight: dict) -> None:
        requirement = self.generation_task_requirement(preflight)
        if requirement["required"] and not requirement["available"]:
            raise RemoteTaskUnavailable(str(requirement["kind"]), str(requirement["reason"]))

    def set_request_actor(self, user_id: str | None) -> None:
        self._request_context.actor_user_id = user_id

    def _request_actor(self) -> str | None:
        return getattr(self._request_context, "actor_user_id", None)

    def _actor_is_authorized(self, task: dict) -> bool:
        actor = task.get("actor_user_id")
        if not actor:
            return not self.require_task_actor
        if self.authorize_actor is None:
            return True
        try:
            return bool(self.authorize_actor(actor))
        except (FileNotFoundError, PermissionError):
            return False

    @contextlib.contextmanager
    def _guard_task_actor(self, task: dict):
        actor = task.get("actor_user_id")
        if not actor:
            yield not self.require_task_actor
            return
        if self.actor_guard is not None:
            stack = contextlib.ExitStack()
            try:
                stack.enter_context(self.actor_guard(actor))
            except (FileNotFoundError, PermissionError):
                yield False
                return
            with stack:
                yield True
            return
        yield True

    def categories(self) -> list[dict]:
        registry = dc.load_registry(self.root)
        counts: dict[str, int] = {}
        for article in self.articles():
            category_id = article.get("primary_category_id")
            if category_id:
                counts[category_id] = counts.get(category_id, 0) + 1
        return [
            {
                **item,
                "id": item["category_id"],
                "label": item["name"],
                "blurb": item["description"],
                "article_count": counts.get(item["category_id"], 0),
                "revision": registry["revision"],
            }
            for item in sorted(registry["categories"], key=lambda row: (row["sort_order"], row["name"].casefold()))
        ]

    def taxonomy(self) -> dict:
        categories = self.categories()
        tags_by_key: dict[str, str] = {}
        for article in self.articles():
            for tag in article.get("tags", []):
                try:
                    clean = dc.validate_tag_name(tag)
                except ValueError:
                    continue
                tags_by_key.setdefault(dc.normalized_key(clean), clean)
        return {
            "categories": [item for item in categories if item["status"] == "active"],
            "archived_categories": [item for item in categories if item["status"] == "archived"],
            "tags": [tags_by_key[key] for key in sorted(tags_by_key)],
        }

    def _plan_taxonomy(
        self,
        registry: dict,
        category: dict | str | None,
        tags: list[str] | None,
        *,
        fallback_tags: list[str] | None = None,
    ) -> tuple[dict | None, list[str], bool]:
        selection = {"kind": "existing", "id": category} if isinstance(category, str) else (category or {"kind": "inbox"})
        if not isinstance(selection, dict) or set(selection) - {"kind", "id", "name"}:
            raise ValueError("invalid category selection")
        kind = selection.get("kind")
        created_category = False
        category_item = None
        if kind == "inbox":
            if selection.keys() != {"kind"}:
                raise ValueError("invalid inbox selection")
        elif kind == "existing":
            category_id = selection.get("id")
            if not isinstance(category_id, str) or not category_id:
                raise ValueError("category selection requires id")
            category_item = next(
                (
                    item
                    for item in registry["categories"]
                    if item["category_id"] == category_id or item["directory_name"] == category_id
                ),
                None,
            )
            if not category_item or category_item["status"] != "active":
                raise ValueError("invalid category")
        elif kind == "create":
            name = dc.validate_name(selection.get("name"))
            match = next(
                (item for item in registry["categories"] if dc.normalized_key(item["name"]) == dc.normalized_key(name)),
                None,
            )
            if match:
                if match["status"] != "active":
                    raise ValueError("matching category is archived")
                category_item = match
            else:
                category_item = dc.new_category(name, sort_order=len(registry["categories"]))
                dc.assert_unique(registry, category_item)
                if (self.root / "wiki" / category_item["directory_name"]).exists():
                    raise ValueError("category directory already exists outside the registry; reconcile it first")
                registry["categories"].append(category_item)
                registry["revision"] += 1
                created_category = True
        else:
            raise ValueError("invalid category selection")

        selected_tags = dc.normalize_tags(tags if tags is not None else (fallback_tags or []))
        known_tags: dict[str, str] = {}
        for article in self.articles():
            for value in article.get("tags", []):
                try:
                    clean = dc.validate_tag_name(value)
                except ValueError:
                    continue
                known_tags.setdefault(dc.normalized_key(clean), clean)
        selected_tags = [known_tags.get(dc.normalized_key(value), value) for value in selected_tags]
        return category_item, selected_tags, created_category

    def _ensure_dynamic_registry(self) -> None:
        """Migrate existing first-level folders without moving user content."""
        with self.files.locked():
            self._ensure_dynamic_registry_file_locked()

    def _ensure_dynamic_registry_file_locked(self) -> None:
        registry_path = self.root / dc.REGISTRY_REL
        if registry_path.is_file():
            return
        wiki_root = self.root / "wiki"
        registry = dc.empty_registry()
        category_for_directory: dict[str, dict] = {}
        directories = sorted(
            path for path in wiki_root.iterdir()
            if path.is_dir() and not path.is_symlink() and path.name != "_inbox" and not path.name.startswith(".")
        )
        for order, directory in enumerate(directories):
            legacy = cats.BY_ID.get(directory.name)
            item = dc.new_category(
                legacy[1] if legacy else directory.name,
                description=legacy[2] if legacy else "",
                directory=directory.name,
                sort_order=order,
            )
            item["legacy_directory"] = bool(legacy and legacy[1] != directory.name)
            dc.assert_unique(registry, item)
            registry["categories"].append(item)
            category_for_directory[directory.name] = item
        registry["revision"] = 1
        changes: dict[str, str] = {dc.REGISTRY_REL: dc.dump_registry(registry)}
        for path in kw.iter_articles(self.root):
            rel = kw.rel_article(self.root, path)
            md = path.read_text(encoding="utf-8")
            directory = Path(rel).parts[0]
            item = category_for_directory.get(directory)
            pending = directory in {"concepts", "_inbox"} or item is None
            updated = dc.ensure_article_metadata(
                md,
                category_id=None if pending else item["category_id"],
                status="pending" if pending else "confirmed",
            )
            if updated != md:
                changes[f"wiki/{rel}"] = updated
        seed = "|".join(sorted(changes))
        operation_id = f"dynamic-category-migration-{hashlib.sha256(seed.encode()).hexdigest()[:12]}-{uuid.uuid4().hex[:12]}"
        log_path = wiki_root / "log.md"
        current_log = log_path.read_text(encoding="utf-8") if log_path.is_file() else "# Wiki Log\n"
        changes["wiki/index.md"] = render_index(self.root, changes)
        changes["wiki/log.md"] = append_log_text(current_log, operation_id=operation_id, kind="dynamic-category-migration", title="workspace")
        self.files.commit(
            changes, kind="dynamic-category-migration",
            metadata={"categories": len(registry["categories"])},
            operation_id=operation_id, _lock_held=True,
        )

    def _recover_path_projections(self) -> None:
        """Finish path projections after a process exits between file commit and SQLite remap."""
        for manifest_path in sorted(self.files.history_root.glob("*/manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if manifest.get("status") != "committed":
                continue
            identities = self._manifest_path_identities(manifest)
            if identities:
                try:
                    self._current_article_path_projections(identities)
                except (FileNotFoundError, RuntimeError, ValueError):
                    continue
                self._project_current_article_paths(identities)

    def _remap_committed_paths(self, operation_id: str, path_map: dict[str, str]) -> None:
        remapped: list[tuple[str, str]] = []
        try:
            manifest = self.files.operation(operation_id)
            identities = self._manifest_path_identities(manifest)
            before_projection = self._current_article_path_projections(identities)
            by_id = {item["article_id"]: item for item in before_projection}
            for identity in identities:
                item = by_id[identity["article_id"]]
                remapped.append((identity["old_path"], item["private_path"]))
            self._project_current_article_paths(identities)
        except BaseException:
            self.files.rollback(operation_id)
            for old, new in reversed(remapped):
                try:
                    restored = self.read_article(old)
                except (FileNotFoundError, ValueError):
                    restored = None
                self.state.remap_article_path(new, old, base_revision=restored["revision"] if restored else None)
            raise

    def _manifest_path_identities(self, manifest: dict) -> list[dict[str, str]]:
        metadata = manifest.get("metadata", {})
        path_map = metadata.get("path_map", {})
        stored = metadata.get("path_projections")
        identities: list[dict[str, str]] = []
        if isinstance(stored, list):
            for item in stored:
                if not isinstance(item, dict):
                    continue
                article_id, old_path = item.get("article_id"), item.get("old_path")
                if isinstance(article_id, str) and isinstance(old_path, str) and old_path in path_map:
                    identities.append({"article_id": article_id, "old_path": old_path})
            if identities:
                return identities
        article_id = metadata.get("article_id")
        for old, new in path_map.items():
            candidate_id = article_id
            if not candidate_id:
                try:
                    candidate_id = self.read_article(new).get("article_id")
                except (FileNotFoundError, RuntimeError, ValueError):
                    candidate_id = None
            if isinstance(candidate_id, str) and candidate_id:
                identities.append({"article_id": candidate_id, "old_path": old})
        return identities

    def _current_article_path_projections(self, identities: list[dict[str, str]]) -> list[dict[str, str]]:
        projections: list[dict[str, str]] = []
        for article_id in dict.fromkeys(item["article_id"] for item in identities):
            resolved = self.resolve_article_id(article_id)
            current = self.read_article(resolved["path"])
            projections.append({
                "article_id": article_id,
                "private_path": current["path"],
                "article_revision": current["revision"],
            })
        return projections

    def _project_current_article_paths(self, identities: list[dict[str, str]]) -> list[dict[str, str]]:
        if not identities:
            return []
        for _attempt in range(8):
            desired = self._current_article_path_projections(identities)
            by_id = {item["article_id"]: item for item in desired}
            for identity in identities:
                current = by_id[identity["article_id"]]
                self.state.remap_article_path(
                    identity["old_path"], current["private_path"],
                    base_revision=current["article_revision"],
                )
            if self.path_remap_callback is not None:
                self.path_remap_callback(desired)
            observed = self._current_article_path_projections(identities)
            if observed == desired:
                return desired
        raise RuntimeError("article path projection did not stabilize")

    def _available_operation_id(self, operation_base: str) -> str:
        operation_id = operation_base
        attempt = 1
        while self.files.operation_slot_exists(operation_id):
            attempt += 1
            operation_id = f"{operation_base}-attempt-{attempt}"
        return operation_id

    def articles(self) -> list[dict]:
        rows = []
        registry = dc.load_registry(self.root)
        categories_by_id = {item["category_id"]: item for item in registry["categories"]}
        for path in kw.iter_articles(self.root):
            rel = kw.rel_article(self.root, path)
            md = path.read_text(encoding="utf-8")
            category_id = dc.meta_value(md, "Category-ID") or None
            category_item = categories_by_id.get(category_id) if category_id else None
            category = category_item["directory_name"] if category_item else "_inbox"
            rows.append(
                {
                    "path": rel,
                    "title": kw.parse_title(md) or path.stem,
                    "aliases": aliases.parse_aliases(md),
                    "category": category,
                    "category_label": cats.label_of(category),
                    "article_id": dc.article_id(md),
                    "primary_category_id": category_id,
                    "tags": dc.tags(md),
                    "classification_status": dc.classification(md) or "pending",
                    "content_status": wiki_ops.parse_status(md) or "词条",
                    "completeness": wiki_ops.structure_completeness(md),
                    "evidence_status": wiki_ops.parse_evidence_status(md),
                }
            )
        ordered_categories = sorted(registry["categories"], key=lambda item: (item["sort_order"], item["name"].casefold()))
        order = {item["category_id"]: index for index, item in enumerate(ordered_categories)}
        rows.sort(key=lambda row: (order.get(row["primary_category_id"], 999), row["title"].casefold()))
        return rows

    def resolve_article_id(self, article_id: str) -> dict:
        matches = [item for item in self.articles() if item.get("article_id") == article_id]
        if len(matches) != 1:
            raise RuntimeError("private imported article requires repair")
        return matches[0]

    def _wiki_path(self, rel: str) -> Path:
        if rel in {"index.md", "log.md"}:
            raise ValueError("reserved wiki file")
        return resolve_relative_file(self.root / "wiki", rel, suffixes={".md"})

    def read_article(self, rel: str) -> dict:
        original = rel
        self._wiki_path(rel)
        final = aliases.follow_redirect(self.root, rel)
        path = self._wiki_path(final)
        md = path.read_text(encoding="utf-8")
        category_id = dc.meta_value(md, "Category-ID") or None
        category_item = None
        if category_id:
            try:
                category_item = dc.category_by_id(dc.load_registry(self.root), category_id)
            except FileNotFoundError:
                category_item = None
        category = category_item["directory_name"] if category_item else "_inbox"
        generation = wiki_ops.parse_generation(md)
        remote_task = None
        task_match = re.search(r"\btask=([a-f0-9]+)", generation or "")
        if task_match:
            try:
                remote_task = self.state.get_task(task_match.group(1))
            except FileNotFoundError:
                remote_task = None
        return {
            "path": final,
            "redirected_from": original if original != final else None,
            "title": kw.parse_title(md) or path.stem,
            "markdown": md,
            "category": category,
            "category_label": category_item["name"] if category_item else "未分类",
            "article_id": dc.article_id(md),
            "primary_category_id": category_id,
            "tags": dc.tags(md),
            "classification_status": dc.classification(md) or "pending",
            "classification_updated_at": dc.meta_value(md, "Classification-Updated"),
            "content_status": wiki_ops.parse_status(md) or "词条",
            "completeness": wiki_ops.structure_completeness(md),
            "missing_sections": wiki_ops.missing_sections(md),
            "evidence_status": wiki_ops.parse_evidence_status(md),
            "generation": generation,
            "remote_task": remote_task,
            "aliases": aliases.parse_aliases(md),
            "sources": meta_values(md, "Sources"),
            "raw": meta_values(md, "Raw"),
            "backlinks": wiki_ops.backlinks(self.root, final),
            "revision": revision(md),
        }

    def read_raw(self, rel: str) -> dict:
        clean = rel.removeprefix("raw/")
        path = resolve_relative_file(self.root / "raw", clean, suffixes=SUPPORTED_SUFFIXES)
        data = path.read_bytes()
        parsed = parse_document_cached(self.root, path.name, data)
        return {
            "path": f"raw/{clean}", "title": kw.parse_title(parsed.markdown) or path.stem,
            "markdown": parsed.markdown, "kind": "raw", "revision": hashlib.sha256(data).hexdigest(),
            "source_format": parsed.source_format, "extracted_chars": parsed.extracted_chars,
            "used_ocr": parsed.used_ocr,
        }

    def preflight_generate(self, keyword: str, *, from_path: str = "", heading: str = "", passage: str = "") -> dict:
        term = kw.normalize(keyword)
        if not term or len(term) > 120 or kw.is_skippable(term):
            raise ValueError("not a usable keyword")
        existing = aliases.resolve(self.root, term)
        excerpts = kw.excerpts_for(self.root, term, extra_needles=[heading] if heading else None)
        evidence = kw.coverage_evidence(excerpts, term)
        result = {
            "keyword": term,
            "existing_path": existing,
            "local_coverage": evidence,
            "context": {"from_path": from_path, "heading": heading, "passage": passage[:800]},
            "excerpts": excerpts,
            "plan": "open_existing" if existing else ("local_generate" if evidence["sufficient"] else "local_draft_then_remote_supplement"),
        }
        result["remote_task"] = self.generation_task_requirement(result)
        return result

    def _draft_markdown(self, term: str, excerpts: list[dict], rel: str, category: str, *, task_id: str | None, evidence_status: str) -> str:
        sources = [item for item in excerpts if not item["path"].startswith("raw/")]
        raws = [item for item in excerpts if item["path"].startswith("raw/")]
        lead = excerpts[0]["text"] if excerpts else f"{term}的本地资料尚不足，当前先保留可阅读的结构化草稿。"
        lead = re.sub(r"\s+", " ", lead).strip()[:500]
        source_line = "; ".join(source_link(rel, item["path"], item["title"]) for item in sources) or "（待补来源）"
        raw_line = "; ".join(source_link(rel, item["path"], item["title"]) for item in raws)
        generation = "local-extractive" + (f"; task={task_id}; state=queued" if task_id else "; state=complete")
        lines = [
            f"# {term}", "", f"> Category: {category}", "> Status: 草稿", f"> Sources: {source_line}",
        ]
        if raw_line:
            lines.append(f"> Raw: {raw_line}")
        lines.extend(
            [
                f"> Updated: {date.today().isoformat()}", f"> Generation: {generation}", f"> Evidence: {evidence_status}", "",
                "## Overview", "", lead, "", "## 它做什么", "", lead, "", "## 怎么用", "",
                f"先根据当前本地来源核对「{term}」的义项，再在具体场景中使用。", "", "## 例子", "",
                f"- 正例：在来源语境一致时使用「{term}」。", f"- 边界：本地证据不足时，不把未核实的细节写成事实。", "", "## See Also", "",
            ]
        )
        lines.extend(f"- {source_link(rel, item['path'], item['title'])}" for item in sources)
        return "\n".join(lines).rstrip() + "\n"

    @serialized_wiki_write
    def generate(
        self,
        keyword: str,
        *,
        from_path: str = "",
        heading: str = "",
        passage: str = "",
        category: dict | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        with self._intent_lock:
            return self._generate_locked(
                keyword,
                from_path=from_path,
                heading=heading,
                passage=passage,
                category=category,
                tags=tags,
            )

    def _generate_locked(
        self,
        keyword: str,
        *,
        from_path: str = "",
        heading: str = "",
        passage: str = "",
        category: dict | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        preflight = self.preflight_generate(keyword, from_path=from_path, heading=heading, passage=passage)
        if preflight["existing_path"]:
            return {"created": False, "task": None, "article": self.read_article(preflight["existing_path"]), "preflight": preflight}
        self.require_generation_task(preflight)
        term = preflight["keyword"]
        registry = dc.load_registry(self.root)
        category_item, selected_tags, created_category = self._plan_taxonomy(registry, category, tags)
        directory = category_item["directory_name"] if category_item else "_inbox"
        rel = f"{directory}/{slugify(term)}.md"
        if (self.root / "wiki" / rel).exists():
            return {"created": False, "task": None, "article": self.read_article(rel), "preflight": preflight}
        needs_remote = not preflight["local_coverage"]["sufficient"]
        needs_llm = self.llm.configured
        task_kind = "supplement" if needs_remote else "generate"
        task = None
        task_payload = {
            "keyword": term,
            "path": rel,
            "category": directory,
            "from_path": from_path,
            "heading": heading,
            "passage": passage[:800],
            "needs_web": needs_remote,
            "needs_llm": needs_llm,
        }
        if needs_remote or needs_llm:
            task, _created = self.state.enqueue_task(
                task_kind, term, task_payload, staged=True, actor_user_id=self._request_actor(),
            )
        evidence_status = "待补证" if needs_remote else "本地已核验"
        md = self._draft_markdown(term, preflight["excerpts"], rel, directory, task_id=task["id"] if task else None, evidence_status=evidence_status)
        md = dc.ensure_article_metadata(
            md,
            category_id=category_item["category_id"] if category_item else None,
            status="confirmed" if category_item else "pending",
            article_tags=selected_tags,
        )
        operation_id = f"generate-{hashlib.sha256((term + revision(md)).encode()).hexdigest()[:20]}"
        log_path = self.root / "wiki" / "log.md"
        log = log_path.read_text(encoding="utf-8") if log_path.exists() else "# Wiki Log\n"
        overrides = {f"wiki/{rel}": md}
        for path in kw.iter_articles(self.root):
            page_rel = kw.rel_article(self.root, path)
            if page_rel == rel:
                continue
            page_md = path.read_text(encoding="utf-8")
            if page_rel != from_path and term.casefold() not in page_md.casefold():
                continue
            updated = wiki_ops.add_see_also(page_md, page_rel, rel, term)
            if updated != page_md:
                overrides[f"wiki/{page_rel}"] = updated
        changes = dict(overrides)
        if created_category:
            changes[dc.REGISTRY_REL] = dc.dump_registry(registry)
        changes.update(
            {
                "wiki/index.md": render_index(self.root, overrides),
                "wiki/log.md": append_log_text(log, operation_id=operation_id, kind="generate", title=term),
            }
        )
        try:
            manifest = self.files.commit(
                changes,
                kind="generate",
                metadata={"title": term, "path": rel},
                operation_id=operation_id,
                directories={f"wiki/{directory}": True},
                _lock_held=True,
            )
        except BaseException:
            if task:
                self.state.delete_staged_task(task["id"])
            raise
        if task:
            payload = task["payload"]
            payload["base_revision"] = revision(md)
            with self.state.connect() as db:
                db.execute("UPDATE tasks SET payload_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), task["id"]))
            task = self.state.activate_task(task["id"])
            self._wake.set()
        article = self.read_article(rel)
        return {"created": True, "task": task, "article": article, "operation_id": manifest["operation_id"], "preflight": preflight}

    def apply_meta(
        self,
        rel: str,
        *,
        category: str | dict,
        status: str,
        tags: list[str] | None = None,
        expected_revision: str | None = None,
        markdown: str | None = None,
    ) -> dict:
        with self._intent_lock:
            return self._apply_meta_locked(
                rel,
                category=category,
                status=status,
                tags=tags,
                expected_revision=expected_revision,
                markdown=markdown,
            )

    def _apply_meta_locked(
        self,
        rel: str,
        *,
        category: str | dict,
        status: str,
        tags: list[str] | None = None,
        expected_revision: str | None = None,
        markdown: str | None = None,
    ) -> dict:
        with self.files.locked():
            committed = self._commit_meta_file_locked(
                rel,
                category=category,
                status=status,
                tags=tags,
                expected_revision=expected_revision,
                markdown=markdown,
            )
        if committed.get("conflict"):
            return committed
        target_rel = committed["target_rel"]
        old_rel = committed["old_rel"]
        operation_id = committed["operation_id"]
        path_map = committed["path_map"]
        updated = self.read_article(target_rel)
        if path_map:
            self._remap_committed_paths(operation_id, path_map)
        else:
            self.state.remap_article_path(old_rel, target_rel, base_revision=updated["revision"])
        return {
            "conflict": False,
            "operation_id": operation_id,
            "article": updated,
            "created_category": committed["created_category"],
            "category": committed["category"],
        }

    def _commit_meta_file_locked(
        self,
        rel: str,
        *,
        category: str | dict,
        status: str,
        tags: list[str] | None = None,
        expected_revision: str | None = None,
        markdown: str | None = None,
    ) -> dict:
        registry = dc.load_registry(self.root)
        if status not in STATUS_VALUES:
            raise ValueError("invalid status")
        article = self.read_article(rel)
        if expected_revision is not None and article["revision"] != expected_revision:
            return {"conflict": True, "disk": article}
        source_markdown = article["markdown"] if markdown is None else markdown
        if not kw.parse_title(source_markdown):
            raise ValueError("article must contain one H1 title")
        if dc.article_id(source_markdown) != article.get("article_id"):
            raise ValueError("Article-ID is immutable")
        selection = category if isinstance(category, dict) else {"kind": "existing", "id": category}
        category_item, selected_tags, created_category = self._plan_taxonomy(
            registry,
            selection,
            tags,
            fallback_tags=article["tags"],
        )
        old_rel = article["path"]
        operation_identity = article["article_id"] or f"legacy:{revision(article['markdown'])}"
        directory = category_item["directory_name"] if category_item else "_inbox"
        md = cats.ensure_category_header(source_markdown, directory)
        md = dc.ensure_article_metadata(
            md,
            category_id=category_item["category_id"] if category_item else None,
            status="confirmed" if category_item else "pending",
            article_uuid=article["article_id"],
            article_tags=selected_tags,
        )
        stable_article_id = dc.article_id(md)
        if not stable_article_id:
            raise RuntimeError("article metadata has no stable Article-ID")
        md = wiki_ops.ensure_status_header(md, status)
        target_rel = old_rel
        changes: dict[str, str | None] = {}
        if Path(old_rel).parent.as_posix() != directory:
            target_rel = f"{directory}/{Path(old_rel).name}"
            counter = 2
            while (self.root / "wiki" / target_rel).exists():
                target_rel = f"{directory}/{Path(old_rel).stem}-{counter}.md"
                counter += 1
            md = wiki_ops.rebase_wiki_hrefs(md, old_rel, target_rel)
            changes[f"wiki/{target_rel}"] = md
            old_title = article["title"]
            redirect_href = wiki_ops.rel_href(old_rel, target_rel)
            alias_line = f"> Aliases: {'; '.join(article['aliases'])}\n" if article["aliases"] else ""
            changes[f"wiki/{old_rel}"] = f"# {old_title}\n\n{alias_line}> Redirect: {redirect_href}\n"
            for path in kw.iter_articles(self.root):
                page_rel = kw.rel_article(self.root, path)
                if page_rel == old_rel:
                    continue
                page_md = path.read_text(encoding="utf-8")
                updated = wiki_ops.rewrite_wiki_hrefs(page_md, page_rel, old_rel, target_rel)
                if updated != page_md:
                    changes[f"wiki/{page_rel}"] = updated
        else:
            changes[f"wiki/{old_rel}"] = md
        operation_seed = "|".join(
            (
                operation_identity,
                old_rel,
                target_rel,
                category_item["category_id"] if category_item else "_inbox",
                status,
                ";".join(dc.normalized_key(value) for value in selected_tags),
            )
        )
        operation_base = f"meta-{hashlib.sha256(operation_seed.encode()).hexdigest()[:20]}"
        operation_id = self._available_operation_id(operation_base)
        path_map = {old_rel: target_rel} if old_rel != target_rel else {}
        log_path = self.root / "wiki" / "log.md"
        if created_category:
            changes[dc.REGISTRY_REL] = dc.dump_registry(registry)
        changes["wiki/index.md"] = render_index(self.root, changes)
        changes["wiki/log.md"] = append_log_text(log_path.read_text(encoding="utf-8") if log_path.exists() else "", operation_id=operation_id, kind="govern", title=article["title"])
        self.files.commit(
            changes,
            kind="meta",
            metadata={
                "source": old_rel,
                "target": target_rel,
                "path_map": path_map,
                "article_id": stable_article_id,
                "path_projections": (
                    [{"article_id": stable_article_id, "old_path": old_rel}] if path_map else []
                ),
            },
            operation_id=operation_id,
            directories={f"wiki/{directory}": True},
            _lock_held=True,
        )
        return {
            "conflict": False,
            "operation_id": operation_id,
            "old_rel": old_rel,
            "target_rel": target_rel,
            "path_map": path_map,
            "created_category": created_category,
            "category": category_item,
        }

    @serialized_wiki_write
    def save_article(
        self,
        rel: str,
        markdown: str,
        expected_revision: str,
        *,
        force: bool = False,
        category: dict | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        if category is not None or tags is not None:
            current = self.read_article(rel)
            return self.apply_meta(
                rel,
                category=category or (
                    {"kind": "existing", "id": current["primary_category_id"]}
                    if current["primary_category_id"]
                    else {"kind": "inbox"}
                ),
                status=wiki_ops.parse_status(markdown) or current["content_status"],
                tags=tags,
                expected_revision=None if force else expected_revision,
                markdown=markdown,
            )
        current = self.read_article(rel)
        if not force and current["revision"] != expected_revision:
            return {"conflict": True, "disk": current}
        title = kw.parse_title(markdown)
        if not title:
            raise ValueError("article must contain one H1 title")
        if dc.article_id(markdown) != current.get("article_id"):
            raise ValueError("Article-ID is immutable")
        if dc.meta_value(markdown, "Category-ID") != (current.get("primary_category_id") or ""):
            raise ValueError("Category-ID can only be changed through the taxonomy selector")
        if dc.classification(markdown) != current.get("classification_status"):
            raise ValueError("Classification is managed by the taxonomy workflow")
        category = cats.parse_category_line(markdown)
        status = wiki_ops.parse_status(markdown)
        if not category or status not in STATUS_VALUES:
            raise ValueError("article must contain valid Category and Status metadata")
        operation_id = f"edit-{hashlib.sha256((rel + revision(markdown)).encode()).hexdigest()[:20]}"
        log_path = self.root / "wiki" / "log.md"
        changes = {
            f"wiki/{rel}": markdown,
            "wiki/index.md": render_index(self.root, {f"wiki/{rel}": markdown}),
            "wiki/log.md": append_log_text(log_path.read_text(encoding="utf-8") if log_path.exists() else "", operation_id=operation_id, kind="edit", title=title),
        }
        self.files.commit(
            changes, kind="edit", metadata={"path": rel},
            operation_id=operation_id, _lock_held=True,
        )
        return {"conflict": False, "operation_id": operation_id, "article": self.read_article(rel)}

    def create_article(
        self,
        title: str,
        body_markdown: str,
        *,
        category: dict | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        with self._intent_lock:
            return self._create_article_locked(title, body_markdown, category=category, tags=tags)

    def _create_article_locked(
        self,
        title: str,
        body_markdown: str,
        *,
        category: dict | None,
        tags: list[str] | None,
    ) -> dict:
        clean_title = unicodedata.normalize("NFKC", title).strip()
        if (
            not clean_title
            or len(clean_title) > 120
            or "\n" in clean_title
            or "\r" in clean_title
            or any(ord(ch) < 32 for ch in clean_title)
        ):
            raise ValueError("invalid title")
        body = body_markdown.lstrip("\ufeff").strip()
        if not body:
            raise ValueError("markdown body is required")
        if contains_h1(body):
            raise ValueError("markdown body cannot contain an H1 title; use the title field")
        if MANAGED_BODY_META_RE.search(body):
            raise ValueError("article metadata is managed by the title and taxonomy fields")

        request_payload = {
            "version": 1,
            "title": clean_title,
            "body": body,
            "category": category or {"kind": "inbox"},
            "tags": tags or [],
        }
        request_hash = hashlib.sha256(
            json.dumps(request_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        operation_base = f"manual-create-{request_hash[:20]}"

        with self.files.locked():
            return self._materialize_manual_article_locked(
                clean_title,
                body,
                category=category,
                tags=tags,
                request_hash=request_hash,
                operation_base=operation_base,
            )

    def _materialize_manual_article_locked(
        self,
        clean_title: str,
        body: str,
        *,
        category: dict | None,
        tags: list[str] | None,
        request_hash: str,
        operation_base: str,
    ) -> dict:

        attempt = 1
        operation_id = operation_base
        while self.files.operation_slot_exists(operation_id):
            try:
                manifest = self.files.operation(operation_id)
            except FileNotFoundError:
                manifest = None
            if manifest and manifest.get("status") == "committed":
                metadata = manifest.get("metadata") or {}
                if metadata.get("request_hash") == request_hash:
                    rel = metadata.get("target")
                    article_id = metadata.get("article_id")
                    if isinstance(rel, str) and isinstance(article_id, str):
                        try:
                            article = self.read_article(rel)
                            target_markdown = self.files.resolve(f"wiki/{rel}").read_text(encoding="utf-8")
                        except (FileNotFoundError, OSError, ValueError):
                            article = None
                            target_markdown = ""
                        if (
                            article
                            and article.get("path") == rel
                            and not article.get("redirected_from")
                            and article.get("article_id") == article_id
                            and not aliases.REDIRECT_RE.search(target_markdown[:4096])
                        ):
                            return {
                                "operation_id": operation_id,
                                "article": article,
                                "created_category": bool(metadata.get("created_category")),
                                "replayed": True,
                            }
                        raise RuntimeError("manual article requires repair")
            attempt += 1
            operation_id = f"{operation_base}-attempt-{attempt}"

        registry = dc.load_registry(self.root)
        category_item, selected_tags, created_category = self._plan_taxonomy(registry, category, tags)
        directory = category_item["directory_name"] if category_item else "_inbox"
        target = f"{directory}/{slugify(clean_title)}.md"
        if aliases.resolve(self.root, clean_title) or (self.root / "wiki" / target).exists():
            raise FileExistsError("an article with this title already exists")

        markdown = f"# {clean_title}\n\n{body.rstrip()}\n"
        markdown = cats.ensure_category_header(markdown, directory)
        markdown = wiki_ops.ensure_status_header(markdown, "草稿")
        markdown = dc.ensure_article_metadata(
            markdown,
            category_id=category_item["category_id"] if category_item else None,
            status="confirmed" if category_item else "pending",
            article_tags=selected_tags,
        )
        article_id = dc.article_id(markdown)
        if not article_id:
            raise RuntimeError("manual article metadata is incomplete")
        if aliases.REDIRECT_RE.search(markdown[:4096]):
            raise ValueError("article metadata is managed by the title and taxonomy fields")

        changes = {f"wiki/{target}": markdown}
        if created_category:
            changes[dc.REGISTRY_REL] = dc.dump_registry(registry)
        changes["wiki/index.md"] = render_index(self.root, changes)
        log_path = self.root / "wiki" / "log.md"
        changes["wiki/log.md"] = append_log_text(
            log_path.read_text(encoding="utf-8") if log_path.exists() else "",
            operation_id=operation_id,
            kind="manual-create",
            title=clean_title,
        )
        try:
            self.files.commit(
                changes,
                kind="manual-create",
                metadata={
                    "request_hash": request_hash,
                    "target": target,
                    "article_id": article_id,
                    "created_category": created_category,
                },
                operation_id=operation_id,
                must_not_exist_paths={f"wiki/{target}"},
                directories={f"wiki/{directory}": True},
                _lock_held=True,
            )
        except TransactionTargetExistsError as exc:
            raise FileExistsError("an article with this title already exists") from exc
        return {
            "operation_id": operation_id,
            "article": self.read_article(target),
            "created_category": created_category,
            "replayed": False,
        }

    @serialized_wiki_write
    def import_public_article(self, intent: dict) -> dict:
        """Materialize one reserved public revision as an independent private draft."""
        with self._intent_lock:
            rel = str(intent["private_path"])
            article_id = str(intent["private_article_id"])
            entry_id = str(intent["public_entry_id"])
            revision_id = str(intent["public_revision_id"])
            operation_base = f"public-import-{intent['id']}"
            operation_id = operation_base
            target = self.root / "wiki" / rel
            if target.is_file():
                article = self.read_article(rel)
                if article.get("article_id") != article_id:
                    raise RuntimeError("public import target is occupied")
                for manifest_path in sorted(self.files.history_root.glob(f"{operation_base}*/manifest.json")):
                    try:
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    metadata = manifest.get("metadata", {})
                    if (
                        manifest.get("status") == "committed"
                        and metadata.get("public_entry_id") == entry_id
                        and metadata.get("public_revision_id") == revision_id
                    ):
                        operation_id = str(manifest.get("operation_id") or operation_id)
                        break
                return {"operation_id": operation_id, "article": article, "replay": True}
            operation_id = self._available_operation_id(operation_base)
            snapshot = intent["snapshot"]
            markdown = str(snapshot.get("markdown") or "")
            if not kw.parse_title(markdown):
                raise ValueError("public revision has no title")
            markdown = dc.ensure_article_metadata(
                markdown, category_id=None, status="pending", article_uuid=article_id,
            )
            markdown = replace_meta(markdown, "Category", "_inbox")
            markdown = replace_meta(markdown, "Status", "草稿")
            markdown = replace_meta(markdown, "Public-Entry", entry_id)
            markdown = replace_meta(markdown, "Public-Revision", revision_id)
            markdown = replace_meta(markdown, "Public-Attribution", str(snapshot.get("attribution") or "匿名用户")[:120])
            markdown = replace_meta(
                markdown, "Public-Reuse-Policy", str(intent["policy_version"]),
            )
            title = kw.parse_title(markdown) or "公开词条"
            log_path = self.root / "wiki" / "log.md"
            changes = {
                f"wiki/{rel}": markdown,
                "wiki/index.md": render_index(self.root, {f"wiki/{rel}": markdown}),
                "wiki/log.md": append_log_text(
                    log_path.read_text(encoding="utf-8") if log_path.exists() else "",
                    operation_id=operation_id, kind="public-import", title=title,
                ),
            }
            try:
                self.files.commit(
                    changes, kind="public-import", operation_id=operation_id,
                    metadata={"public_entry_id": entry_id, "public_revision_id": revision_id},
                    must_not_exist_paths={f"wiki/{rel}"},
                    _lock_held=True,
                )
            except FileExistsError:
                if target.is_file() and self.read_article(rel).get("article_id") == article_id:
                    return {"operation_id": operation_id, "article": self.read_article(rel), "replay": True}
                raise
            return {"operation_id": operation_id, "article": self.read_article(rel), "replay": False}

    @staticmethod
    def _canonical_text_hash(text: str) -> str:
        text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def upload_raw(self, filename: str, content: str | bytes) -> dict:
        if not isinstance(filename, str) or not filename.strip() or len(filename) > 255:
            raise ValueError("invalid filename")
        if not isinstance(content, (str, bytes)) or not content:
            raise ValueError("Raw content cannot be empty")
        data = content.encode("utf-8") if isinstance(content, str) else content
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError("Raw file exceeds 10 MiB")
        name = Path(filename.strip()).name
        if name != filename.strip() or name.startswith(".") or any(ord(ch) < 32 for ch in name):
            raise ValueError("invalid filename")
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise ValueError("不支持该文件格式，未加入原料箱")
        parsed = parse_document_cached(self.root, name, data)

        digest = hashlib.sha256(data).hexdigest()
        text_digest = self._canonical_text_hash(parsed.markdown)
        for item in self.raw_inbox():
            if item.get("byte_hash") == digest or item.get("text_hash") == text_digest:
                return {"created": False, "raw": item}

        stem = Path(name).stem[:160].strip(" .") or "material"
        candidate = f"local/{stem}{suffix}"
        counter = 2
        operation_id = f"raw-upload-{digest[:12]}-{uuid.uuid4().hex}"
        while True:
            try:
                self.files.commit(
                    {f"raw/{candidate}": data},
                    kind="raw-upload",
                    metadata={"filename": name, "path": f"raw/{candidate}"},
                    operation_id=operation_id,
                    must_not_exist=True,
                )
                break
            except FileExistsError:
                existing = next((item for item in self.raw_inbox() if item.get("byte_hash") == digest), None)
                if existing is not None:
                    return {"created": False, "raw": existing}
                candidate = f"local/{stem}-{counter}{suffix}"
                counter += 1
        item = {
            "path": f"raw/{candidate}", "size": len(data), "ingestable": True, "reason": None,
            "title": kw.parse_title(parsed.markdown) or Path(name).stem, "byte_hash": digest,
            "text_hash": text_digest, "source_format": parsed.source_format,
            "extracted_chars": parsed.extracted_chars, "used_ocr": parsed.used_ocr,
            "status": "unlinked", "linked_target": None,
        }
        return {"created": True, "operation_id": operation_id, "raw": item}

    def raw_inbox(self) -> list[dict]:
        records = {row["path"]: row for row in self.state.raw_records()}
        by_hash = {row["byte_hash"]: row for row in records.values()}
        by_text = {row["text_hash"]: row for row in records.values()}
        linked_targets: dict[str, str] = {}
        for article_path in kw.iter_articles(self.root):
            article_rel = kw.rel_article(self.root, article_path)
            article_md = article_path.read_text(encoding="utf-8")
            for href in meta_values(article_md, "Raw"):
                target = href if href.startswith("raw/") else wiki_ops.resolve_href(article_rel, href)
                if target and target.startswith("raw/"):
                    linked_targets.setdefault(target, article_rel)
        out = []
        raw_root = self.root / "raw"
        for path in sorted(raw_root.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            rel = path.relative_to(self.root).as_posix()
            if any(part.startswith(".") for part in path.relative_to(raw_root).parts):
                continue
            size = path.stat().st_size
            item = {"path": rel, "size": size, "ingestable": True, "reason": None, "status": "unlinked"}
            if size > MAX_INPUT_BYTES:
                item.update(ingestable=False, reason="Raw 文件超过 10 MiB", status="rejected")
                out.append(item)
                continue
            data = path.read_bytes()
            try:
                parsed = parse_document_cached(self.root, path.name, data)
                text_hash = self._canonical_text_hash(parsed.markdown)
            except ValueError as exc:
                item.update(ingestable=False, reason=str(exc), status="rejected")
                out.append(item)
                continue
            byte_hash = hashlib.sha256(data).hexdigest()
            record = records.get(rel)
            duplicate = by_hash.get(byte_hash) or by_text.get(text_hash)
            linked_target = record["target_path"] if record else linked_targets.get(rel)
            if linked_target:
                try:
                    linked_target = aliases.follow_redirect(self.root, linked_target)
                except (FileNotFoundError, ValueError):
                    pass
            item.update(
                {
                    "title": kw.parse_title(parsed.markdown) or path.stem,
                    "byte_hash": byte_hash,
                    "text_hash": text_hash,
                    "source_format": parsed.source_format,
                    "extracted_chars": parsed.extracted_chars,
                    "used_ocr": parsed.used_ocr,
                    "status": "integrity_changed" if record and record["byte_hash"] != byte_hash else ("ingested" if linked_target else ("duplicate" if duplicate else "unlinked")),
                    "linked_target": linked_target or (duplicate["target_path"] if duplicate else None),
                }
            )
            out.append(item)
        return out

    def ingest_preview(self, raw_path: str) -> dict:
        item = next((entry for entry in self.raw_inbox() if entry["path"] == raw_path), None)
        if not item:
            raise FileNotFoundError(raw_path)
        if not item["ingestable"]:
            raise ValueError(item["reason"])
        raw = self.read_raw(raw_path)
        title = raw["title"]
        existing = aliases.resolve(self.root, title)
        suggestion = "duplicate" if item["status"] in {"duplicate", "ingested"} else ("supplement" if existing else ("seed" if not self.articles() else "new"))
        return {
            "raw": item, "markdown": raw["markdown"], "suggested_title": title,
            "suggested_disposition": suggestion, "suggested_target": existing or item.get("linked_target"),
            "preview_changes": ["Sources", "Raw", "index", "log"],
        }

    @staticmethod
    def _seed_markdown(markdown: str, *, category: str, raw_link: str) -> str:
        """Adopt a complete document as Wiki content without summarizing or rewriting it."""
        md = markdown.lstrip("\ufeff")
        if not kw.parse_title(md):
            raise ValueError("Wiki seed must contain an H1 title")
        md = cats.ensure_category_header(md, category)
        md = wiki_ops.ensure_status_header(md, "词条")
        md = replace_meta(md, "Raw", raw_link)
        md = replace_meta(md, "Generation", "seed-adopted; source-preserved=true")
        md = replace_meta(md, "Evidence", "Raw 原文")
        return md.rstrip() + "\n"

    @serialized_wiki_write
    def ingest_commit(
        self,
        raw_path: str,
        disposition: str,
        *,
        title: str,
        category: dict | None = None,
        tags: list[str] | None = None,
        target_path: str | None = None,
    ) -> dict:
        with self._intent_lock:
            return self._ingest_commit_locked(
                raw_path,
                disposition,
                title=title,
                category=category,
                tags=tags,
                target_path=target_path,
            )

    def _ingest_commit_locked(
        self,
        raw_path: str,
        disposition: str,
        *,
        title: str,
        category: dict | None,
        tags: list[str] | None,
        target_path: str | None,
    ) -> dict:
        if disposition not in {"seed", "new", "supplement", "duplicate", "defer"}:
            raise ValueError("invalid disposition")
        preview = self.ingest_preview(raw_path)
        item = preview["raw"]
        if item["status"] == "integrity_changed":
            raise ValueError("Raw file changed after it was recorded; save it as a new version")
        if not title or len(title) > 120:
            raise ValueError("invalid title")
        if item["status"] in {"ingested", "duplicate"} and disposition not in {"seed", "duplicate", "defer"}:
            raise ValueError("Raw content has already been recorded")
        if disposition in {"new", "supplement"} and self.llm.configured:
            self.require_remote_task("governance")
        raw = self.read_raw(raw_path)
        operation_base = f"ingest-{item['byte_hash'][:20]}-{disposition}"
        operation_id = operation_base
        changes: dict[str, str] = {}
        result_target = target_path
        registry = dc.load_registry(self.root)
        category_item = None
        selected_tags: list[str] = []
        created_category = False
        directory = "_inbox"
        if disposition in {"seed", "new"}:
            category_item, selected_tags, created_category = self._plan_taxonomy(registry, category, tags)
            directory = category_item["directory_name"] if category_item else "_inbox"
        if disposition == "seed":
            existing_target = target_path or item.get("linked_target") or aliases.resolve(self.root, title)
            if existing_target:
                raise ValueError("canonical article already exists; use supplement")
            result_target = f"{directory}/{slugify(title)}.md"
            if (self.root / "wiki" / result_target).exists():
                raise ValueError("canonical article already exists; use supplement")
            raw_link = source_link(result_target, raw_path, raw["title"])
            seed_md = self._seed_markdown(raw["markdown"], category=directory, raw_link=raw_link)
            changes[f"wiki/{result_target}"] = dc.ensure_article_metadata(
                seed_md,
                category_id=category_item["category_id"] if category_item else None,
                status="confirmed" if category_item else "pending",
                article_tags=selected_tags,
            )
        elif disposition == "new":
            result_target = f"{directory}/{slugify(title)}.md"
            if aliases.resolve(self.root, title) or (self.root / "wiki" / result_target).exists():
                raise ValueError("canonical article already exists; use supplement")
            excerpt = [{"title": raw["title"], "path": raw_path, "text": raw["markdown"][:900]}]
            md = self._draft_markdown(title, excerpt, result_target, directory, task_id=None, evidence_status="Raw 已核验")
            changes[f"wiki/{result_target}"] = dc.ensure_article_metadata(
                md,
                category_id=category_item["category_id"] if category_item else None,
                status="confirmed" if category_item else "pending",
                article_tags=selected_tags,
            )
        elif disposition == "supplement":
            if not target_path:
                raise ValueError("supplement requires a target article")
            article = self.read_article(target_path)
            result_target = article["path"]
            link = source_link(result_target, raw_path, raw["title"])
            existing_raw = re.search(r"^>\s*Raw:\s*(.*)$", article["markdown"], re.M | re.I)
            value = existing_raw.group(1).strip() if existing_raw else ""
            if link not in value:
                value = "; ".join(part for part in [value, link] if part)
            changes[f"wiki/{result_target}"] = replace_meta(article["markdown"], "Raw", value)
        elif disposition in {"duplicate", "defer"}:
            result_target = target_path or item.get("linked_target")
        if disposition in {"duplicate", "defer"}:
            self.state.record_raw(raw_path, item["byte_hash"], item["text_hash"], disposition, result_target, operation_id)
            return {"operation_id": operation_id, "disposition": disposition, "target_path": result_target, "article": None}
        operation_id = self._available_operation_id(operation_base)
        log_path = self.root / "wiki" / "log.md"
        if created_category:
            changes[dc.REGISTRY_REL] = dc.dump_registry(registry)
        changes["wiki/index.md"] = render_index(self.root, changes)
        changes["wiki/log.md"] = append_log_text(log_path.read_text(encoding="utf-8") if log_path.exists() else "", operation_id=operation_id, kind="ingest", title=title)
        try:
            manifest = self.files.commit(
                changes,
                kind="ingest",
                metadata={"raw": raw_path, "target": result_target, "disposition": disposition},
                operation_id=operation_id,
                must_not_exist_paths=(
                    {f"wiki/{result_target}"} if disposition in {"seed", "new"} else None
                ),
                directories={f"wiki/{directory}": True} if disposition in {"seed", "new"} else None,
                _lock_held=True,
            )
        except OperationExistsError:
            raise
        except TransactionTargetExistsError as exc:
            if disposition in {"seed", "new"}:
                raise ValueError("canonical article already exists; use supplement") from exc
            raise
        self.state.record_raw(raw_path, item["byte_hash"], item["text_hash"], disposition, result_target, operation_id)
        article = self.read_article(result_target) if result_target else None
        task = None
        if article and self.llm.configured and disposition != "seed":
            task, _created = self.state.enqueue_task(
                "governance",
                article["path"],
                {
                    "path": article["path"],
                    "base_revision": article["revision"],
                    "trigger": "raw_ingest",
                },
                actor_user_id=self._request_actor(),
            )
            self._wake.set()
        return {
            "operation_id": manifest["operation_id"],
            "disposition": disposition,
            "target_path": article["path"] if article else result_target,
            "article": article,
            "task": task,
        }

    def merge_preview(self, source: str, target: str) -> dict:
        source_article = self.read_article(source)
        target_article = self.read_article(target)
        if source_article["path"] == target_article["path"]:
            raise ValueError("source and target must differ")
        source_rel, target_rel = source_article["path"], target_article["path"]
        merged_aliases = list(dict.fromkeys([*target_article["aliases"], source_article["title"], *source_article["aliases"]]))
        merged_sources = self._merge_meta_paths(source_article, target_article, "sources")
        merged_raw = self._merge_meta_paths(source_article, target_article, "raw")
        inbound = wiki_ops.backlinks(self.root, source_rel)
        return {"source": source_article, "target": target_article, "merged": {"aliases": merged_aliases, "sources": merged_sources, "raw": merged_raw}, "inbound_changes": inbound, "redirect": {"from": source_rel, "to": target_rel}, "recoverable": True}

    def _merge_meta_paths(self, source: dict, target: dict, key: str) -> list[str]:
        merged: list[str] = []
        for article in (target, source):
            for href in article[key]:
                if href.startswith(("http://", "https://", "mailto:")):
                    normalized = href
                else:
                    canonical = wiki_ops.resolve_href(article["path"], href)
                    if not canonical:
                        continue
                    normalized = wiki_ops.rel_href(target["path"], canonical)
                if normalized not in merged:
                    merged.append(normalized)
        return merged

    @serialized_wiki_write
    def merge_commit(self, source: str, target: str) -> dict:
        preview = self.merge_preview(source, target)
        source_article, target_article = preview["source"], preview["target"]
        source_rel, target_rel = source_article["path"], target_article["path"]
        target_md = aliases.ensure_aliases_header(target_article["markdown"], preview["merged"]["aliases"])
        # Source/Raw links remain canonical relative to the target article.
        if preview["merged"]["sources"]:
            target_md = replace_meta(target_md, "Sources", "; ".join(preview["merged"]["sources"]))
        if preview["merged"]["raw"]:
            target_md = replace_meta(target_md, "Raw", "; ".join(preview["merged"]["raw"]))
        redirect_href = wiki_ops.rel_href(source_rel, target_rel)
        redirect_md = f"# {source_article['title']}\n\n> Aliases: {'; '.join(source_article['aliases'])}\n> Redirect: {redirect_href}\n"
        changes: dict[str, str] = {f"wiki/{target_rel}": target_md, f"wiki/{source_rel}": redirect_md}
        wiki_root = self.root / "wiki"
        for redirect_path in sorted(wiki_root.rglob("*.md")):
            redirect_rel = redirect_path.relative_to(wiki_root).as_posix()
            if redirect_rel == source_rel:
                continue
            redirect_source = redirect_path.read_text(encoding="utf-8")
            match = aliases.REDIRECT_RE.search(redirect_source[:4096])
            if not match:
                continue
            try:
                redirect_target = aliases._resolve_redirect_rel(redirect_rel, match.group(1).strip())
                final_target = aliases.follow_redirect(self.root, redirect_target)
            except ValueError:
                continue
            if final_target == source_rel:
                direct = wiki_ops.rel_href(redirect_rel, target_rel)
                changes[f"wiki/{redirect_rel}"] = aliases.REDIRECT_RE.sub(f"> Redirect: {direct}", redirect_source, count=1)
        for path in kw.iter_articles(self.root):
            rel = kw.rel_article(self.root, path)
            if rel in {source_rel, target_rel}:
                continue
            md = path.read_text(encoding="utf-8")
            updated = wiki_ops.rewrite_wiki_hrefs(md, rel, source_rel, target_rel)
            if updated != md:
                changes[f"wiki/{rel}"] = updated
        operation_id = f"merge-{hashlib.sha256((source_rel + target_rel + revision(target_md)).encode()).hexdigest()[:20]}"
        log_path = self.root / "wiki" / "log.md"
        changes["wiki/index.md"] = render_index(self.root, changes)
        changes["wiki/log.md"] = append_log_text(log_path.read_text(encoding="utf-8") if log_path.exists() else "", operation_id=operation_id, kind="merge", title=f"{source_article['title']} -> {target_article['title']}")
        self.files.commit(
            changes, kind="merge", metadata={"source": source_rel, "target": target_rel},
            operation_id=operation_id, _lock_held=True,
        )
        return {"operation_id": operation_id, "article": self.read_article(target_rel)}

    def rollback(self, operation_id: str) -> dict:
        before = self.files.operation(operation_id)
        manifest = self.files.rollback(operation_id)
        path_map = before.get("metadata", {}).get("path_map", {})
        for old, new in path_map.items():
            try:
                restored = self.read_article(old)
            except (FileNotFoundError, ValueError):
                restored = None
            self.state.remap_article_path(new, old, base_revision=restored["revision"] if restored else None)
        return manifest

    def retry_task(self, task_id: str) -> dict:
        with self._intent_lock:
            task = self.state.get_task(task_id)
            if task.get("error_type") == "feature_removed":
                raise ValueError("task kind is no longer supported")
            self.require_remote_task(task["kind"])
            payload = dict(task["payload"])
            if payload.get("path"):
                payload["base_revision"] = self.read_article(str(payload["path"]))["revision"]
            task = self.state.retry_task(task_id, payload=payload, actor_user_id=self._request_actor())
            self._wake.set()
            return task

    def cancel_task(self, task_id: str) -> dict:
        with self._intent_lock:
            return self.state.cancel_task(task_id)

    def enqueue_governance(self) -> dict:
        if not self.llm.configured:
            raise ValueError("AI governance requires a configured model")
        self.require_remote_task("governance")
        actionable = {"content_quality", "dead_link", "missing_backlink"}
        paths = sorted({item["path"] for item in wiki_ops.lint_wiki(self.root) if item["kind"] in actionable})
        tasks = []
        queued = 0
        for rel in paths:
            article = self.read_article(rel)
            task, created = self.state.enqueue_task(
                "governance",
                rel,
                {"path": rel, "base_revision": article["revision"]},
                actor_user_id=self._request_actor(),
            )
            tasks.append(task)
            queued += int(created)
        if queued:
            self._wake.set()
        return {"queued": queued, "tasks": tasks}

    def category_preview(
        self,
        action: str,
        *,
        category_id: str = "",
        target_category_id: str = "",
        name: str = "",
        description: str = "",
        sort_order: int | None = None,
    ) -> dict:
        if action not in {"rename", "archive", "restore", "delete", "reorder"}:
            raise ValueError("invalid category action")
        registry = dc.load_registry(self.root)
        conflicts = []
        before = None
        after = None
        affected: list[str] = []
        before = dict(dc.category_by_id(registry, category_id))
        after = dict(before)
        folder = self.root / "wiki" / before["directory_name"]
        affected = [item["path"] for item in self.articles() if item["primary_category_id"] == category_id]
        if action == "rename":
            after.update(name=dc.validate_name(name), directory_name=dc.directory_name(name), description=description.strip()[:500] if description else before["description"], updated_at=dc.now_iso(), legacy_directory=False)
            dc.assert_unique(registry, after, ignore_id=category_id)
            target = self.root / "wiki" / after["directory_name"]
            if target.exists() and target.resolve() != folder.resolve():
                conflicts.append({"kind": "target_exists", "path": after["directory_name"]})
            unknown = [path.name for path in folder.iterdir() if not path.is_file() or path.suffix.lower() != ".md"] if folder.is_dir() else []
            if unknown:
                conflicts.append({"kind": "unknown_files", "paths": unknown})
        elif action == "archive":
            after.update(status="archived", updated_at=dc.now_iso())
        elif action == "restore":
            after.update(status="active", updated_at=dc.now_iso())
        elif action == "delete":
            after = None
            if affected:
                conflicts.append({"kind": "category_not_empty", "article_count": len(affected)})
            if folder.is_dir() and any(folder.iterdir()):
                conflicts.append({"kind": "directory_not_empty"})
        elif action == "reorder":
            if sort_order is None or sort_order < 0:
                raise ValueError("invalid sort order")
            after.update(sort_order=sort_order, updated_at=dc.now_iso())
        payload = {"taxonomy_revision": registry["revision"], "action": action, "category_id": category_id, "before": before, "after": after}
        stored = self.state.create_preview("category", payload)
        return {"preview_id": stored["preview_id"], "expires_at": stored["expires_at"], "action": action, "before": before, "after": after, "affected_articles": affected, "conflicts": conflicts, "can_commit": not conflicts}

    def category_commit(self, preview_id: str) -> dict:
        stored = self.state.get_preview(preview_id, "category")
        payload = stored["payload"]
        with self._intent_lock:
            with self.files.locked():
                committed = self._category_commit_file_locked(payload)
            self._remap_committed_paths(committed["operation_id"], committed["path_map"])
            self.state.consume_preview(preview_id)
            return committed

    def _category_commit_file_locked(self, payload: dict) -> dict:
        registry = dc.load_registry(self.root)
        if registry["revision"] != payload["taxonomy_revision"]:
            raise RuntimeError("category preview is stale")
        action = payload["action"]
        if action not in {"rename", "archive", "restore", "delete", "reorder"}:
            raise ValueError("invalid category action")
        before, after = payload.get("before"), payload.get("after")
        next_registry = json.loads(json.dumps(registry, ensure_ascii=False))
        current = dc.category_by_id(next_registry, payload["category_id"])
        if before != current:
            raise RuntimeError("category changed after preview")
        if action == "delete":
            if any(item["primary_category_id"] == current["category_id"] for item in self.articles()):
                raise RuntimeError("category is not empty")
            next_registry["categories"] = [item for item in next_registry["categories"] if item["category_id"] != current["category_id"]]
        else:
            dc.assert_unique(next_registry, after, ignore_id=current["category_id"])
            next_registry["categories"] = [after if item["category_id"] == current["category_id"] else item for item in next_registry["categories"]]
        next_registry["revision"] += 1
        changes: dict[str, str | None] = {dc.REGISTRY_REL: dc.dump_registry(next_registry)}
        directories: dict[str, bool] = {}
        path_map: dict[str, str] = {}
        path_projections: list[dict[str, str]] = []
        if action == "delete":
            folder = self.root / "wiki" / before["directory_name"]
            if not folder.is_dir() or any(folder.iterdir()):
                raise RuntimeError("category directory is not empty")
            directories[f"wiki/{before['directory_name']}"] = False
        elif action == "rename" and before["directory_name"] != after["directory_name"]:
            old_dir, new_dir = before["directory_name"], after["directory_name"]
            target_dir = self.root / "wiki" / new_dir
            if target_dir.exists():
                raise FileExistsError(new_dir)
            directories[f"wiki/{new_dir}"] = True
            for article in [item for item in self.articles() if item["primary_category_id"] == before["category_id"]]:
                old, new = article["path"], f"{new_dir}/{Path(article['path']).name}"
                current = self.read_article(old)
                md = cats.ensure_category_header(current["markdown"], new_dir)
                md = wiki_ops.rebase_wiki_hrefs(md, old, new)
                changes[f"wiki/{new}"] = md
                changes[f"wiki/{old}"] = None
                path_map[old] = new
                if not article.get("article_id"):
                    raise RuntimeError("moved article has no stable Article-ID")
                path_projections.append({"article_id": article["article_id"], "old_path": old})
            for path in kw.iter_articles(self.root):
                rel = kw.rel_article(self.root, path)
                if rel in path_map:
                    continue
                md = path.read_text(encoding="utf-8")
                updated = md
                for old, new in path_map.items():
                    updated = wiki_ops.rewrite_wiki_hrefs(updated, rel, old, new)
                if updated != md:
                    changes[f"wiki/{rel}"] = updated
            directories[f"wiki/{old_dir}"] = False
        operation_id = f"category-{action}-{uuid.uuid4().hex}"
        log_path = self.root / "wiki" / "log.md"
        changes["wiki/index.md"] = render_index(self.root, changes)
        changes["wiki/log.md"] = append_log_text(log_path.read_text(encoding="utf-8") if log_path.exists() else "", operation_id=operation_id, kind=f"category-{action}", title=(after or before)["name"])
        self.files.commit(
            changes, directories=directories, kind=f"category-{action}",
            metadata={
                "path_map": path_map,
                "path_projections": path_projections,
                "category_id": (after or before)["category_id"],
            },
            operation_id=operation_id, _lock_held=True,
        )
        return {"operation_id": operation_id, "category": after, "path_map": path_map}

    def scan_reconciliation(self) -> dict:
        with self._intent_lock:
            return self._scan_reconciliation_locked()

    def _scan_reconciliation_locked(self) -> dict:
        registry = dc.load_registry(self.root)
        registered = {dc.normalized_key(item["directory_name"]): item for item in registry["categories"]}
        items = []
        wiki_root = self.root / "wiki"
        articles = self.articles()
        for path in wiki_root.iterdir():
            if not path.is_dir() or path.name == "_inbox" or path.name.startswith("."):
                continue
            if path.is_symlink():
                items.append({"fingerprint": f"symlink:{path.name}", "kind": "unsafe_symlink", "path": path.name, "available_actions": ["defer"]})
            elif dc.normalized_key(path.name) not in registered:
                affected_paths = [item.relative_to(wiki_root).as_posix() for item in sorted(path.glob("*.md")) if item.is_file() and not item.is_symlink()]
                items.append({
                    "fingerprint": f"new-dir:{path.name}", "kind": "new_directory", "path": path.name,
                    "application_record": None, "disk_state": {"path": path.name, "exists": True},
                    "affected_paths": affected_paths, "available_actions": ["adopt", "defer"],
                })
        for item in registry["categories"]:
            path = wiki_root / item["directory_name"]
            if not path.is_dir():
                affected_paths = [article["path"] for article in articles if article["primary_category_id"] == item["category_id"]]
                items.append({
                    "fingerprint": f"missing-dir:{item['category_id']}", "kind": "missing_directory",
                    "category_id": item["category_id"], "path": item["directory_name"],
                    "application_record": {"category_id": item["category_id"], "directory_name": item["directory_name"]},
                    "disk_state": {"path": item["directory_name"], "exists": False},
                    "affected_paths": affected_paths, "available_actions": ["restore", "defer"],
                })
        by_article_id: dict[str, list[str]] = {}
        for article in articles:
            if article.get("article_id"):
                by_article_id.setdefault(article["article_id"], []).append(article["path"])
        for article_id, paths in by_article_id.items():
            if len(paths) > 1:
                items.append({"fingerprint": f"duplicate-article-id:{article_id}:{'|'.join(sorted(paths))}", "kind": "duplicate_article_id", "article_id": article_id, "paths": sorted(paths), "available_actions": ["defer"]})
        for article in articles:
            if not article["primary_category_id"]:
                continue
            try:
                category = dc.category_by_id(registry, article["primary_category_id"])
            except FileNotFoundError:
                continue
            if Path(article["path"]).parts[0] != category["directory_name"]:
                items.append({
                    "fingerprint": f"article-moved:{article['article_id']}:{article['path']}", "kind": "article_moved",
                    "article_id": article["article_id"], "path": article["path"], "expected_directory": category["directory_name"],
                    "application_record": {"article_id": article["article_id"], "expected_directory": category["directory_name"]},
                    "disk_state": {"path": article["path"], "exists": True}, "affected_paths": [article["path"]],
                    "available_actions": ["adopt", "restore", "defer"],
                })
        records = self.state.replace_reconciliation(items)
        return {"count": len(records), "items": records}

    def reconciliation_preview(self, item_id: str, decision: str) -> dict:
        if decision not in {"adopt", "restore", "defer"}:
            raise ValueError("invalid reconciliation decision")
        item = self.state.get_reconciliation(item_id)
        if item["status"] != "pending" or decision not in item["payload"].get("available_actions", []):
            raise ValueError("decision is not available")
        conflicts = []
        payload = item["payload"]
        if payload["kind"] == "unsafe_symlink" and decision != "defer":
            conflicts.append({"kind": "unsafe_symlink"})
        stored = self.state.create_preview("reconciliation", {"item_id": item_id, "decision": decision, "fingerprint": item["fingerprint"], "detected": payload})
        return {"preview_id": stored["preview_id"], "decision": decision, "changes": payload, "conflicts": conflicts, "can_commit": not conflicts}

    def reconciliation_commit(self, preview_id: str) -> dict:
        with self._intent_lock:
            stored = self.state.get_preview(preview_id, "reconciliation")
            payload = stored["payload"]
            item = self.state.get_reconciliation(payload["item_id"])
            if (
                item["status"] != "pending"
                or item["fingerprint"] != payload["fingerprint"]
                or item["payload"] != payload["detected"]
            ):
                raise RuntimeError("reconciliation preview is stale")
            decision = payload["decision"]
            if decision == "defer":
                result = self.state.resolve_reconciliation(item["id"], "deferred")
                self.state.consume_preview(preview_id)
                return {"operation_id": None, "item": result}
            with self.files.locked():
                committed = self._reconciliation_commit_file_locked(item, decision)
            self._remap_committed_paths(committed["operation_id"], committed["path_map"])
            result = self.state.resolve_reconciliation(
                item["id"], "adopted" if decision == "adopt" else "restored",
            )
            self.state.consume_preview(preview_id)
            return {"operation_id": committed["operation_id"], "item": result}

    def _reconciliation_commit_file_locked(self, item: dict, decision: str) -> dict:
        detected = item["payload"]
        registry = dc.load_registry(self.root)
        next_registry = json.loads(json.dumps(registry, ensure_ascii=False))
        changes: dict[str, str | None] = {}
        directories: dict[str, bool] = {}
        path_map: dict[str, str] = {}
        path_projections: list[dict[str, str]] = []
        if detected["kind"] == "new_directory" and decision == "adopt":
            folder = self.root / "wiki" / detected["path"]
            if not folder.is_dir() or folder.is_symlink():
                raise RuntimeError("detected directory changed")
            category = dc.new_category(detected["path"], directory=detected["path"], sort_order=len(next_registry["categories"]))
            dc.assert_unique(next_registry, category)
            next_registry["categories"].append(category)
            for article_path in sorted(folder.glob("*.md")):
                if article_path.is_symlink():
                    raise ValueError("symlinked articles cannot be adopted")
                rel = article_path.relative_to(self.root / "wiki").as_posix()
                md = article_path.read_text(encoding="utf-8")
                changes[f"wiki/{rel}"] = dc.ensure_article_metadata(md, category_id=category["category_id"], status="confirmed")
        elif detected["kind"] == "missing_directory" and decision == "restore":
            category = dc.category_by_id(registry, detected["category_id"])
            if (self.root / "wiki" / category["directory_name"]).exists():
                raise RuntimeError("directory changed after scan")
            directories[f"wiki/{category['directory_name']}"] = True
        elif detected["kind"] == "article_moved":
            article = next((entry for entry in self.articles() if entry["article_id"] == detected["article_id"]), None)
            if not article or article["path"] != detected["path"]:
                raise RuntimeError("article changed after scan")
            current = self.read_article(article["path"])
            if decision == "adopt":
                actual_dir = Path(article["path"]).parts[0]
                category = dc.category_by_directory(registry, actual_dir)
                if not category:
                    raise ValueError("adopt the external category first")
                changes[f"wiki/{article['path']}"] = dc.ensure_article_metadata(current["markdown"], category_id=category["category_id"], status="confirmed", article_uuid=current["article_id"], article_tags=current["tags"])
            else:
                target = f"{detected['expected_directory']}/{Path(article['path']).name}"
                if (self.root / "wiki" / target).exists():
                    raise FileExistsError(target)
                md = wiki_ops.rebase_wiki_hrefs(current["markdown"], article["path"], target)
                changes[f"wiki/{target}"] = md
                changes[f"wiki/{article['path']}"] = None
                path_map[article["path"]] = target
                path_projections.append({"article_id": article["article_id"], "old_path": article["path"]})
        else:
            raise ValueError("unsupported reconciliation action")
        taxonomy_changed = detected["kind"] == "new_directory" and decision == "adopt"
        if taxonomy_changed:
            next_registry["revision"] += 1
            changes[dc.REGISTRY_REL] = dc.dump_registry(next_registry)
        operation_id = f"reconcile-{uuid.uuid4().hex}"
        log_path = self.root / "wiki" / "log.md"
        changes["wiki/index.md"] = render_index(self.root, changes)
        changes["wiki/log.md"] = append_log_text(log_path.read_text(encoding="utf-8") if log_path.exists() else "", operation_id=operation_id, kind="reconcile", title=detected["kind"])
        self.files.commit(
            changes, directories=directories, kind="reconciliation",
            metadata={
                "path_map": path_map,
                "path_projections": path_projections,
                "reconciliation_id": item["id"],
            },
            operation_id=operation_id, _lock_held=True,
        )
        return {"operation_id": operation_id, "path_map": path_map}

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            task = self.state.claim_task(self.remote_task_kinds)
            if not task:
                self._wake.wait(1)
                self._wake.clear()
                continue
            try:
                if not self._actor_is_authorized(task):
                    raise RemoteError("auth_revoked", "The task initiator no longer has write access.", retryable=False)
                result = self._run_remote_task(task)
                self._finalize_remote_result(task, result)
            except RemoteError as exc:
                self._finalize_remote_failure(task, exc.code, str(exc), retry=exc.retryable)
            except Exception as exc:
                self._finalize_remote_failure(task, "model_error", str(exc), retry=False)

    def _finalize_remote_failure(
        self, task: dict, error_type: str, error_message: str, *, retry: bool,
    ) -> dict:
        with self._intent_lock:
            current_task = self.state.get_task(task["id"])
            if (
                self._retired
                or current_task["status"] != "running"
                or current_task["attempts"] != task["attempts"]
            ):
                return current_task
            with self._guard_task_actor(task) as actor_authorized:
                if self._retired:
                    return self.state.get_task(task["id"])
                if not actor_authorized:
                    error_type = "auth_revoked"
                    error_message = "The task initiator no longer has write access."
                    retry = False
                current_task = self.state.fail_task(
                    task["id"], error_type, error_message,
                    retry=retry, expected_attempt=task["attempts"],
                )
                if (
                    current_task["status"] not in {"queued", "failed"}
                    or current_task["attempts"] != task["attempts"]
                ):
                    return current_task
                return current_task

    def _finalize_remote_result(self, task: dict, result: dict) -> dict:
        with self._intent_lock:
            current_task = self.state.get_task(task["id"])
            if self._retired:
                return current_task
            with self._guard_task_actor(task) as actor_authorized:
                return self._finalize_remote_result_locked(task, result, current_task, actor_authorized)

    def _finalize_remote_result_locked(
        self, task: dict, result: dict, current_task: dict, actor_authorized: bool,
    ) -> dict:
        if current_task["status"] != "running" or current_task["attempts"] != task["attempts"]:
            return current_task
        if not actor_authorized:
            current_task = self.state.fail_task(
                task["id"], "auth_revoked", "The task initiator no longer has write access.",
                retry=False, expected_attempt=task["attempts"],
            )
            return current_task
        stale = bool(result.get("stale"))
        if stale:
            message = "The source or category system changed while the task was running."
            current_task = self.state.fail_task(
                task["id"], "stale_revision", message, retry=False, expected_attempt=task["attempts"],
            )
            if current_task["status"] != "failed" or current_task["attempts"] != task["attempts"]:
                return current_task
            return current_task
        if result.get("cancelled"):
            return self.state.cancel_task(task["id"])
        return self.state.complete_task(task["id"], result, expected_attempt=task["attempts"])

    def _run_remote_task(self, task: dict) -> dict:
        if task["kind"] == "governance":
            return self._run_governance_task(task)
        payload = task["payload"]
        if self.state.get_task(task["id"])["status"] == "cancelled":
            return {"cancelled": True}
        pages: list[dict] = []
        if payload.get("needs_web"):
            query = " ".join(part for part in [payload["keyword"], payload.get("heading", "")] if part)[:80]
            pages = self.remote_search(query, limit=3, keyword=payload["keyword"])
            if not pages:
                raise RemoteError("no_results", "No usable public sources were found.", retryable=False)
        current = self.read_article(payload["path"])
        base_revision = payload.get("base_revision") or current["revision"]
        if current["revision"] != base_revision:
            return {"conflict": True, "path": payload["path"], "proposal": None, "reason": "article_changed"}
        raw_changes: dict[str, str] = {}
        excerpts = [
            item
            for item in kw.excerpts_for(self.root, payload["keyword"], extra_needles=[payload.get("heading", "")])
            if item["path"] != payload["path"]
        ]
        for page in pages:
            raw_rel = websearch.raw_path(page)
            raw_changes[raw_rel] = websearch.raw_markdown(page)
            excerpts.append({"title": page["title"], "path": raw_rel, "text": page["text"][:900]})
        if payload.get("needs_llm"):
            proposal = self._call_llm(payload, excerpts)
            generation = "web+llm" if pages else "local+llm"
        elif task["kind"] == "supplement":
            proposal = current["markdown"]
            generation = "web-supplement"
        else:
            proposal = self._draft_markdown(payload["keyword"], excerpts, payload["path"], payload["category"], task_id=None, evidence_status="远端已补证")
            generation = "web-extractive"
        proposal = replace_meta(proposal, "Generation", f"{generation}; task={task['id']}; state=succeeded")
        proposal = replace_meta(proposal, "Evidence", "远端已补证" if pages else "本地已核验")
        wiki_sources = list({item["path"]: item for item in excerpts if not item["path"].startswith("raw/")}.values())
        raw_sources = list({item["path"]: item for item in excerpts if item["path"].startswith("raw/")}.values())
        if wiki_sources and task["kind"] != "supplement":
            proposal = replace_meta(proposal, "Sources", "; ".join(source_link(payload["path"], item["path"], item["title"]) for item in wiki_sources))
        if raw_sources:
            raw_links = list(current["raw"]) if task["kind"] == "supplement" else []
            for item in raw_sources:
                link = source_link(payload["path"], item["path"], item["title"])
                if link not in raw_links:
                    raw_links.append(link)
            proposal = replace_meta(proposal, "Raw", "; ".join(raw_links))
        proposal = self._normalize_ai_article(
            proposal,
            payload["path"],
            payload["keyword"],
            payload["category"],
            extra_paths=set(raw_changes),
        )
        proposal = dc.ensure_article_metadata(
            proposal,
            category_id=current.get("primary_category_id"),
            status=current.get("classification_status") or "pending",
            article_uuid=current.get("article_id"),
            article_tags=current.get("tags", []),
        )
        quality = wiki_ops.article_quality_issues(self.root, payload["path"], proposal, extra_paths=set(raw_changes))
        if quality:
            raise RemoteError("quality_gate", "AI output failed quality checks: " + "; ".join(quality), retryable=False)
        with self._intent_lock:
            if self._retired:
                return {"superseded": True, "path": payload["path"]}
            with self._guard_task_actor(task) as actor_authorized:
                return self._commit_remote_article_task(task, payload, base_revision, proposal, raw_changes, actor_authorized)

    @serialized_wiki_write
    def _commit_remote_article_task(
        self, task: dict, payload: dict, base_revision: str, proposal: str,
        raw_changes: dict[str, str], actor_authorized: bool,
    ) -> dict:
        if not actor_authorized:
            raise RemoteError("auth_revoked", "The task initiator no longer has write access.", retryable=False)
        latest = self.read_article(payload["path"])
        if latest["revision"] != base_revision:
            return {"conflict": True, "path": payload["path"], "proposal": proposal, "reason": "article_changed"}
        current_task = self.state.get_task(task["id"])
        if current_task["status"] != "running" or current_task["attempts"] != task["attempts"]:
            return {"superseded": True, "path": payload["path"]}
        operation_id = f"task-{task['id']}-attempt-{task['attempts']}"
        log_path = self.root / "wiki" / "log.md"
        changes = {**raw_changes, f"wiki/{payload['path']}": proposal, "wiki/index.md": render_index(self.root, {f"wiki/{payload['path']}": proposal})}
        changes["wiki/log.md"] = append_log_text(log_path.read_text(encoding="utf-8") if log_path.exists() else "", operation_id=operation_id, kind="remote-complete", title=payload["keyword"])
        self.files.commit(
            changes, kind="remote-complete", metadata={"task": task["id"], "path": payload["path"]},
            operation_id=operation_id, _lock_held=True,
        )
        result = {"conflict": False, "path": payload["path"], "operation_id": operation_id, "raw_paths": list(raw_changes)}
        try:
            finalized = self.state.complete_task(task["id"], result, expected_attempt=task["attempts"])
        except BaseException:
            current_task = self.state.get_task(task["id"])
            if current_task["status"] != "succeeded" or current_task["attempts"] != task["attempts"]:
                self.files.rollback(operation_id)
            raise
        if finalized["status"] != "succeeded" or finalized["attempts"] != task["attempts"]:
            self.files.rollback(operation_id)
            return {"superseded": True, "path": payload["path"]}
        return result

    def _normalize_ai_article(
        self,
        md: str,
        rel: str,
        title: str,
        category: str,
        *,
        extra_paths: set[str] | None = None,
    ) -> str:
        lines = []
        for line in md.splitlines():
            if re.match(r"^>\s*\*\*(?:Category|Status)\*\*\s*:", line, re.I):
                continue
            lines.append(line)
        md = "\n".join(lines).rstrip() + "\n"
        md = re.sub(r"^#\s+.*$", f"# {title}", md, count=1, flags=re.M)
        md = cats.ensure_category_header(md, category)
        status = wiki_ops.parse_status(md)
        md = wiki_ops.ensure_status_header(md, status if status in STATUS_VALUES else "草稿")
        heading_aliases = {
            "它做什么": ("解释", "工作原理", "核心机制", "原理", "功能与机制"),
            "怎么用": ("如何使用", "使用方法", "实践方法", "操作方法"),
            "例子": ("示例", "案例", "使用示例", "应用案例"),
        }
        for canonical, alternatives in heading_aliases.items():
            if wiki_ops.has_heading(md, canonical) or (canonical == "它做什么" and wiki_ops.has_heading(md, "机制")):
                continue
            for alternative in alternatives:
                pattern = re.compile(rf"^##\s+{re.escape(alternative)}\s*$", re.M)
                if pattern.search(md):
                    md = pattern.sub(f"## {canonical}", md, count=1)
                    break
        if not (wiki_ops.has_heading(md, "它做什么") or wiki_ops.has_heading(md, "机制")):
            overview = re.search(r"^##\s+Overview\s*\n+(.*?)(?=^##\s+|\Z)", md, re.M | re.S)
            grounded = overview.group(1).strip() if overview else article_summary(md)
            if grounded:
                marker = re.search(r"^##\s+", md, re.M)
                insert_at = marker.start() if marker else len(md)
                md = md[:insert_at].rstrip() + f"\n\n## 它做什么\n\n{grounded}\n\n" + md[insert_at:].lstrip()

        def safe_link(match: re.Match[str]) -> str:
            label, href = match.groups()
            if href.startswith(("http://", "https://", "mailto:", "#")):
                return match.group(0)
            target = wiki_ops.resolve_href(rel, href)
            if not target:
                return label
            path = self.root / target if target.startswith("raw/") else self.root / "wiki" / target
            return match.group(0) if path.is_file() or target in (extra_paths or set()) else label

        md = MD_LINK_RE.sub(safe_link, md)
        for article in self.articles():
            target = article["path"]
            if target == rel or not wiki_ops.mentions_title(md, article["title"]):
                continue
            md = wiki_ops.add_see_also(md, rel, target, article["title"])
        return md.rstrip() + "\n"

    def _run_governance_task(self, task: dict) -> dict:
        payload = task["payload"]
        current = self.read_article(payload["path"])
        if current["revision"] != payload["base_revision"]:
            return {"conflict": True, "path": payload["path"], "reason": "article_changed"}
        proposal = self._call_governance_llm(current)
        proposal = self._normalize_ai_article(proposal, current["path"], current["title"], current["category"])
        for key in ("Aliases", "Sources", "Raw", "Evidence", "Status"):
            original = re.search(rf"^>\s*{key}:\s*(.*?)\s*$", current["markdown"], re.M | re.I)
            if original and (key != "Status" or original.group(1).strip() in STATUS_VALUES):
                proposal = replace_meta(proposal, key, original.group(1).strip())
        if wiki_ops.parse_status(proposal) not in STATUS_VALUES:
            proposal = wiki_ops.ensure_status_header(proposal, "草稿")
        proposal = replace_meta(proposal, "Generation", f"ai-governed; task={task['id']}; state=succeeded")
        quality = wiki_ops.article_quality_issues(self.root, current["path"], proposal)
        if quality:
            raise RemoteError("quality_gate", "AI output failed quality checks: " + "; ".join(quality), retryable=False)
        with self._intent_lock:
            if self._retired:
                return {"superseded": True, "path": current["path"]}
            with self._guard_task_actor(task) as actor_authorized:
                return self._commit_governance_task(task, payload, current, proposal, actor_authorized)

    @serialized_wiki_write
    def _commit_governance_task(
        self, task: dict, payload: dict, current: dict, proposal: str, actor_authorized: bool,
    ) -> dict:
        if not actor_authorized:
            raise RemoteError(
                "auth_revoked", "The task initiator no longer has write access.", retryable=False)
        latest = self.read_article(current["path"])
        if latest["revision"] != payload["base_revision"]:
            return {"conflict": True, "path": current["path"], "proposal": proposal, "reason": "article_changed"}
        current_task = self.state.get_task(task["id"])
        if current_task["status"] != "running" or current_task["attempts"] != task["attempts"]:
            return {"superseded": True, "path": current["path"]}
        operation_id = f"govern-{task['id']}-attempt-{task['attempts']}"
        log_path = self.root / "wiki" / "log.md"
        changes = {
            f"wiki/{current['path']}": proposal,
            "wiki/index.md": render_index(self.root, {f"wiki/{current['path']}": proposal}),
            "wiki/log.md": append_log_text(
                log_path.read_text(encoding="utf-8") if log_path.exists() else "",
                operation_id=operation_id,
                kind="ai-govern",
                title=current["title"],
            ),
        }
        self.files.commit(
            changes,
            kind="ai-govern",
            metadata={"task": task["id"], "path": current["path"]},
            operation_id=operation_id,
            _lock_held=True,
        )
        result = {
            "conflict": False,
            "path": current["path"],
            "operation_id": operation_id,
        }
        try:
            finalized = self.state.complete_task(
                task["id"], result, expected_attempt=task["attempts"])
        except BaseException:
            current_task = self.state.get_task(task["id"])
            if current_task["status"] != "succeeded" or current_task["attempts"] != task["attempts"]:
                self.files.rollback(operation_id)
            raise
        if finalized["status"] != "succeeded" or finalized["attempts"] != task["attempts"]:
            self.files.rollback(operation_id)
            return {"superseded": True, "path": current["path"]}
        return result

    def _call_governance_llm(self, article: dict) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RemoteError("not_configured", "The OpenAI-compatible client is not installed.") from exc
        raw_evidence: list[str] = []
        remaining = 14_400
        for href in article.get("raw", []):
            target = href if href.startswith("raw/") else wiki_ops.resolve_href(article["path"], href)
            if not target or not target.startswith("raw/") or remaining <= 0:
                continue
            try:
                raw = self.read_raw(target)
            except (FileNotFoundError, ValueError):
                continue
            excerpt = raw["markdown"][:remaining]
            raw_evidence.append(f"[Raw: {raw['title']}]\n{excerpt}")
            remaining -= len(excerpt)
        evidence = "\n\n".join(raw_evidence)
        prompt = (
            "修订下面的中文 Markdown 知识库词条。保留有依据的事实和原有引用，不新增无法由正文或来源支持的事实。"
            "输出完整 Markdown，不输出代码围栏。必须保留一个 H1，并包含 Category、Status、Sources 或 Raw 元数据，"
            "以及“它做什么”或“机制”、“怎么用”、“例子”、“See Also”章节。"
            "不得创建不存在的内部链接；不确定内容要明确标注。\n\n"
            + article["markdown"]
            + ("\n\n以下是该词条关联的 Raw 原文摘录，应优先据此补全内容：\n\n" + evidence if evidence else "")
        )
        client = OpenAI(api_key=self.llm.api_key or "not-needed", base_url=self.llm.base_url, timeout=30, max_retries=0)
        return self._complete_markdown(client, prompt, article["title"])

    def _complete_markdown(self, client: object, prompt: str, title: str) -> str:
        system = "你是知识库编辑。最终回复只能是完整 Markdown 正文，第一行必须是一个 `# 标题`；不要输出分析、解释或代码围栏。"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        for attempt in range(2):
            response = client.chat.completions.create(model=self.llm.model, messages=messages, max_tokens=4096)
            try:
                message = response.choices[0].message
            except (AttributeError, IndexError):
                raw = ""
            else:
                raw = model_message_text(message)
            try:
                return extract_markdown_article(raw, fallback_title=title)
            except RemoteError:
                if attempt:
                    raise RemoteError(
                        "model_error",
                        "The model did not return a Markdown article after format correction.",
                        retryable=True,
                    )
                messages = [
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": prompt + f"\n\n严格重写：第一行必须是 `# {title}`，然后直接给出完整正文。",
                    },
                ]
        raise AssertionError("unreachable")

    def _call_llm(self, payload: dict, excerpts: list[dict]) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RemoteError("not_configured", "The OpenAI-compatible client is not installed.") from exc
        packed = "\n\n".join(f"[{item['title']}] {item['text'][:900]}" for item in excerpts[:16])
        prompt = (
            f"为本地知识库写一篇中文 Markdown 词条：{payload['keyword']}。\n"
            f"Category 必须是 {payload['category']}，Status 设为草稿。必须包含 Overview、它做什么、怎么用、例子、See Also。"
            "只使用下面资料，不确定内容明确标注，不输出 HTML。\n\n" + packed
        )
        client = OpenAI(api_key=self.llm.api_key or "not-needed", base_url=self.llm.base_url, timeout=30, max_retries=0)
        text = self._complete_markdown(client, prompt, payload["keyword"])
        text = cats.ensure_category_header(text, payload["category"])
        text = wiki_ops.ensure_status_header(text, wiki_ops.parse_status(text) or "草稿")
        return text.rstrip() + "\n"
