"""Application service for reading, governing, and generating the Markdown wiki."""

from __future__ import annotations

import hashlib
import contextlib
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
from storage import FileStore

STATUS_VALUES = {"词条", "草稿", "过时", "有争议"}
META_LINE_RE = re.compile(r"^>\s*([A-Za-z]+):\s*(.*?)\s*$", re.M)
MD_LINK_RE = wiki_ops.MD_LINK_RE


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
    display_categories = [*categories, {"category_id": "_inbox", "name": "待归类", "description": "尚未确认主分类的词条"}]
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


class WikiService:
    def __init__(
        self,
        project_root: Path,
        *,
        llm_config: LLMConfig | None = None,
        remote_search: Callable[..., list[dict]] | None = None,
        start_worker: bool = True,
        remote_task_kinds: set[str] | None = None,
        authorize_actor: Callable[[str], bool] | None = None,
        actor_guard: Callable[[str], object] | None = None,
        require_task_actor: bool = False,
        path_remap_callback: Callable[[dict[str, str]], None] | None = None,
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
        self.remote_task_kinds = remote_task_kinds
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

    def _ensure_dynamic_registry(self) -> None:
        """Migrate existing first-level folders without moving user content."""
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
        self.files.commit(changes, kind="dynamic-category-migration", metadata={"categories": len(registry["categories"])}, operation_id=operation_id)

    def _recover_path_projections(self) -> None:
        """Finish path projections after a process exits between file commit and SQLite remap."""
        for manifest_path in sorted(self.files.history_root.glob("*/manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if manifest.get("status") != "committed":
                continue
            recovered_paths: dict[str, str] = {}
            article_id = manifest.get("metadata", {}).get("article_id")
            for old, new in manifest.get("metadata", {}).get("path_map", {}).items():
                if not article_id and not (self.root / "wiki" / new).is_file():
                    continue
                try:
                    resolved = self.resolve_article_id(article_id) if article_id else self.read_article(new)
                    current = self.read_article(resolved["path"])
                    if article_id and current.get("article_id") != article_id:
                        continue
                except (FileNotFoundError, RuntimeError, ValueError):
                    current = None
                if current is None:
                    continue
                current_path = current["path"]
                self.state.remap_article_path(old, current_path, base_revision=current["revision"])
                recovered_paths[old] = current_path
            if recovered_paths and self.path_remap_callback is not None:
                self.path_remap_callback(recovered_paths)

    def _remap_committed_paths(self, operation_id: str, path_map: dict[str, str]) -> None:
        remapped: list[tuple[str, str]] = []
        try:
            for old, new in path_map.items():
                self.state.remap_article_path(old, new, base_revision=self.read_article(new)["revision"])
                remapped.append((old, new))
            if self.path_remap_callback is not None:
                self.path_remap_callback(path_map)
        except BaseException:
            self.files.rollback(operation_id)
            for old, new in reversed(remapped):
                try:
                    restored = self.read_article(old)
                except (FileNotFoundError, ValueError):
                    restored = None
                self.state.remap_article_path(new, old, base_revision=restored["revision"] if restored else None)
            raise

    def _available_operation_id(self, operation_base: str) -> str:
        operation_id = operation_base
        attempt = 1
        while True:
            try:
                self.files.operation(operation_id)
            except FileNotFoundError:
                return operation_id
            attempt += 1
            operation_id = f"{operation_base}-attempt-{attempt}"

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
            "category_label": category_item["name"] if category_item else "待归类",
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
        expected = self._expected_classification(term, " ".join([heading, passage]))
        return {
            "keyword": term,
            "existing_path": existing,
            "category": expected["directory_name"] if expected else "_inbox",
            "category_label": expected["name"] if expected else "待归类",
            "expected_classification": expected,
            "local_coverage": evidence,
            "context": {"from_path": from_path, "heading": heading, "passage": passage[:800]},
            "excerpts": excerpts,
            "plan": "open_existing" if existing else ("local_generate" if evidence["sufficient"] else "local_draft_then_remote_supplement"),
        }

    def _expected_classification(self, title: str, body: str) -> dict | None:
        registry = dc.load_registry(self.root)
        blob = unicodedata.normalize("NFKC", f"{title} {body}").casefold()
        ranked: list[tuple[int, dict]] = []
        for item in registry["categories"]:
            if item["status"] != "active":
                continue
            needles = [item["name"], item.get("description", ""), *item.get("aliases", [])]
            score = sum(2 if dc.normalized_key(value) in dc.normalized_key(title) else 1 for value in needles if value and dc.normalized_key(value) in blob)
            if score:
                ranked.append((score, item))
        if not ranked:
            return None
        score, item = max(ranked, key=lambda pair: (pair[0], -pair[1]["sort_order"]))
        return {"category_id": item["category_id"], "name": item["name"], "directory_name": item["directory_name"], "confidence": min(0.95, 0.55 + score * 0.1), "reason": "标题或上下文与该分类的名称、说明相符", "preview_only": True}

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

    def generate(self, keyword: str, *, from_path: str = "", heading: str = "", passage: str = "") -> dict:
        with self._intent_lock:
            return self._generate_locked(keyword, from_path=from_path, heading=heading, passage=passage)

    def _generate_locked(self, keyword: str, *, from_path: str = "", heading: str = "", passage: str = "") -> dict:
        preflight = self.preflight_generate(keyword, from_path=from_path, heading=heading, passage=passage)
        if preflight["existing_path"]:
            return {"created": False, "task": None, "article": self.read_article(preflight["existing_path"]), "preflight": preflight}
        term = preflight["keyword"]
        category = "_inbox"
        rel = f"_inbox/{slugify(term)}.md"
        if (self.root / "wiki" / rel).exists():
            return {"created": False, "task": None, "article": self.read_article(rel), "preflight": preflight}
        needs_remote = not preflight["local_coverage"]["sufficient"]
        needs_llm = self.llm.configured
        task_kind = "supplement" if needs_remote else "generate"
        task = None
        task_payload = {
            "keyword": term,
            "path": rel,
            "category": category,
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
        md = self._draft_markdown(term, preflight["excerpts"], rel, category, task_id=task["id"] if task else None, evidence_status=evidence_status)
        md = dc.ensure_article_metadata(md, category_id=None, status="pending")
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
        changes.update(
            {
                "wiki/index.md": render_index(self.root, overrides),
                "wiki/log.md": append_log_text(log, operation_id=operation_id, kind="generate", title=term),
            }
        )
        try:
            manifest = self.files.commit(changes, kind="generate", metadata={"title": term, "path": rel}, operation_id=operation_id)
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
        classification_task = None
        if not task:
            classification_task = self.enqueue_classification(article)
        return {"created": True, "task": task, "classification_task": classification_task, "article": article, "operation_id": manifest["operation_id"], "preflight": preflight}

    def apply_meta(self, rel: str, *, category: str, status: str) -> dict:
        with self._intent_lock:
            return self._apply_meta_locked(rel, category=category, status=status)

    def _apply_meta_locked(self, rel: str, *, category: str, status: str) -> dict:
        registry = dc.load_registry(self.root)
        category_item = next(
            (item for item in registry["categories"] if item["category_id"] == category or item["directory_name"] == category),
            None,
        )
        if not category_item or category_item["status"] != "active":
            raise ValueError("invalid category")
        if status not in STATUS_VALUES:
            raise ValueError("invalid status")
        article = self.read_article(rel)
        old_rel = article["path"]
        category = category_item["directory_name"]
        md = cats.ensure_category_header(article["markdown"], category)
        md = dc.ensure_article_metadata(
            md, category_id=category_item["category_id"], status="confirmed",
            article_uuid=article["article_id"], article_tags=article["tags"],
        )
        md = wiki_ops.ensure_status_header(md, status)
        target_rel = old_rel
        changes: dict[str, str | None] = {}
        if Path(old_rel).parent.as_posix() != category:
            target_rel = f"{category}/{Path(old_rel).name}"
            counter = 2
            while (self.root / "wiki" / target_rel).exists():
                target_rel = f"{category}/{Path(old_rel).stem}-{counter}.md"
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
        operation_base = f"meta-{hashlib.sha256((old_rel + target_rel + revision(md)).encode()).hexdigest()[:20]}"
        operation_id = self._available_operation_id(operation_base)
        path_map = {old_rel: target_rel} if old_rel != target_rel else {}
        log_path = self.root / "wiki" / "log.md"
        changes["wiki/index.md"] = render_index(self.root, changes)
        changes["wiki/log.md"] = append_log_text(log_path.read_text(encoding="utf-8") if log_path.exists() else "", operation_id=operation_id, kind="govern", title=article["title"])
        self.files.commit(
            changes,
            kind="meta",
            metadata={
                "source": old_rel,
                "target": target_rel,
                "path_map": path_map,
                "article_id": article["article_id"],
            },
            operation_id=operation_id,
        )
        updated = self.read_article(target_rel)
        if path_map:
            self._remap_committed_paths(operation_id, path_map)
        else:
            self.state.remap_article_path(old_rel, target_rel, base_revision=updated["revision"])
        return {"operation_id": operation_id, "article": updated}

    def save_article(self, rel: str, markdown: str, expected_revision: str, *, force: bool = False) -> dict:
        current = self.read_article(rel)
        if not force and current["revision"] != expected_revision:
            return {"conflict": True, "disk": current}
        title = kw.parse_title(markdown)
        if not title:
            raise ValueError("article must contain one H1 title")
        if dc.article_id(markdown) != current.get("article_id"):
            raise ValueError("Article-ID is immutable")
        if dc.meta_value(markdown, "Category-ID") != (current.get("primary_category_id") or ""):
            raise ValueError("Category-ID can only be changed through classification")
        if dc.classification(markdown) != current.get("classification_status"):
            raise ValueError("Classification can only be changed through classification")
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
        self.files.commit(changes, kind="edit", metadata={"path": rel}, operation_id=operation_id)
        return {"conflict": False, "operation_id": operation_id, "article": self.read_article(rel)}

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
        plan_task = self.enqueue_raw_classification_plan(item) if self.llm.configured else None
        return {"created": True, "operation_id": operation_id, "raw": item, "classification_plan_task": plan_task}

    def enqueue_raw_classification_plan(self, item: dict) -> dict | None:
        if not self.llm.configured:
            return None
        registry = dc.load_registry(self.root)
        raw_path = item["path"]
        raw_revision = item["byte_hash"]
        existing = self.state.raw_classification_plan(raw_path, raw_revision, registry["revision"])
        if existing and existing["status"] in {"queued", "running", "succeeded"}:
            return self.state.get_task(existing["task_id"]) if existing.get("task_id") else None
        task, _created = self.state.enqueue_task(
            "raw-classification-plan",
            f"{raw_path}:{raw_revision}:{registry['revision']}",
            {"raw_path": raw_path, "raw_revision": raw_revision, "taxonomy_revision": registry["revision"]},
            actor_user_id=self._request_actor(),
        )
        self.state.save_raw_classification_plan(raw_path, raw_revision, registry["revision"], "queued", task_id=task["id"])
        self._wake.set()
        return task

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
        expected = self._expected_classification(title, raw["markdown"][:50000])
        registry = dc.load_registry(self.root)
        remote_plan = self.state.raw_classification_plan(raw_path, item["byte_hash"], registry["revision"])
        if not remote_plan and self.llm.configured:
            self.enqueue_raw_classification_plan(item)
            remote_plan = self.state.raw_classification_plan(raw_path, item["byte_hash"], registry["revision"])
        if remote_plan and remote_plan.get("task_id") and remote_plan["status"] in {"queued", "running"}:
            try:
                task_state = self.state.get_task(remote_plan["task_id"])
                remote_plan["status"] = task_state["status"]
                remote_plan["error_type"] = task_state.get("error_type")
                remote_plan["error_message"] = task_state.get("error_message")
            except FileNotFoundError:
                pass
        suggestion = "duplicate" if item["status"] in {"duplicate", "ingested"} else ("supplement" if existing else ("seed" if not self.articles() else "new"))
        return {
            "raw": item, "markdown": raw["markdown"], "suggested_title": title,
            "suggested_category": expected["category_id"] if expected else None,
            "classification_plan": {
                "status": remote_plan["status"] if remote_plan else "local_fallback",
                "task_id": remote_plan.get("task_id") if remote_plan else None,
                "error_type": remote_plan.get("error_type") if remote_plan else None,
                "error_message": remote_plan.get("error_message") if remote_plan else None,
                "candidates": (remote_plan.get("plan") or {}).get("candidates", []) if remote_plan else ([expected] if expected else []),
                "notice": "预计分类仅供预览，不会创建目录；正文生成后会基于完整内容重新归类。",
            },
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

    def ingest_commit(self, raw_path: str, disposition: str, *, title: str, category: str = "", target_path: str | None = None) -> dict:
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
        raw = self.read_raw(raw_path)
        operation_id = f"ingest-{item['byte_hash'][:20]}-{disposition}"
        changes: dict[str, str] = {}
        result_target = target_path
        if disposition == "seed":
            existing_target = target_path or item.get("linked_target") or aliases.resolve(self.root, title)
            result_target = self.read_article(existing_target)["path"] if existing_target else f"_inbox/{slugify(title)}.md"
            raw_link = source_link(result_target, raw_path, raw["title"])
            seed_md = self._seed_markdown(raw["markdown"], category="_inbox", raw_link=raw_link)
            changes[f"wiki/{result_target}"] = dc.ensure_article_metadata(seed_md, category_id=None, status="pending")
        elif disposition == "new":
            result_target = f"_inbox/{slugify(title)}.md"
            if aliases.resolve(self.root, title) or (self.root / "wiki" / result_target).exists():
                raise ValueError("canonical article already exists; use supplement")
            excerpt = [{"title": raw["title"], "path": raw_path, "text": raw["markdown"][:900]}]
            md = self._draft_markdown(title, excerpt, result_target, "_inbox", task_id=None, evidence_status="Raw 已核验")
            changes[f"wiki/{result_target}"] = dc.ensure_article_metadata(md, category_id=None, status="pending")
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
        log_path = self.root / "wiki" / "log.md"
        changes["wiki/index.md"] = render_index(self.root, changes)
        changes["wiki/log.md"] = append_log_text(log_path.read_text(encoding="utf-8") if log_path.exists() else "", operation_id=operation_id, kind="ingest", title=title)
        manifest = self.files.commit(changes, kind="ingest", metadata={"raw": raw_path, "target": result_target, "disposition": disposition}, operation_id=operation_id)
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
        classification_task = self.enqueue_classification(article) if article and disposition in {"seed", "new"} else None
        return {
            "operation_id": manifest["operation_id"],
            "disposition": disposition,
            "target_path": article["path"] if article else result_target,
            "article": article,
            "task": task,
            "classification_task": classification_task,
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
        self.files.commit(changes, kind="merge", metadata={"source": source_rel, "target": target_rel}, operation_id=operation_id)
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

    def enqueue_classification(self, article: dict, *, actor_user_id: str | None = None) -> dict | None:
        article_id = article.get("article_id")
        if not article_id:
            raise ValueError("article has no stable id")
        registry = dc.load_registry(self.root)
        if not self.llm.configured:
            self.state.save_classification_suggestion(
                article_id, article["revision"], registry["revision"], "failed",
                error_type="not_configured", error_message="请先在设置中配置模型，或手动选择分类。",
            )
            return None
        subject = f"{article_id}:{article['revision']}:{registry['revision']}"
        task, _created = self.state.enqueue_task(
            "article-classification",
            subject,
            {
                "article_id": article_id,
                "path": article["path"],
                "article_revision": article["revision"],
                "taxonomy_revision": registry["revision"],
            },
            actor_user_id=actor_user_id if actor_user_id is not None else self._request_actor(),
        )
        self.state.save_classification_suggestion(
            article_id, article["revision"], registry["revision"], "queued", task_id=task["id"]
        )
        self._wake.set()
        return task

    def classification_workbench(self) -> dict:
        registry = dc.load_registry(self.root)
        items = []
        counts = {"high_confidence": 0, "needs_confirmation": 0, "new_category": 0, "failed": 0}
        for article in self.articles():
            if article["classification_status"] != "pending":
                continue
            path = self._wiki_path(article["path"])
            md = path.read_text(encoding="utf-8")
            article_revision = revision(md)
            record = self.state.classification_suggestion(article["article_id"], article_revision, registry["revision"])
            if record and record.get("task_id") and record["status"] in {"queued", "running"}:
                try:
                    task_state = self.state.get_task(record["task_id"])
                    record["status"] = task_state["status"]
                    record["error_type"] = task_state.get("error_type")
                    record["error_message"] = task_state.get("error_message")
                except FileNotFoundError:
                    pass
            suggestion = record.get("suggestion") if record else None
            if record and record["status"] == "failed":
                group = "failed"
            elif suggestion and suggestion.get("new_category"):
                group = "new_category"
            elif suggestion and suggestion.get("candidates") and suggestion["candidates"][0]["confidence"] >= 0.85:
                group = "high_confidence"
            else:
                group = "needs_confirmation"
            counts[group] += 1
            items.append({"article": {**article, "revision": article_revision, "summary": article_summary(md)}, "suggestion": record, "group": group})
        return {"counts": counts, "high_confidence_threshold": 0.85, "taxonomy_revision": registry["revision"], "draft": self.state.classification_draft(), "items": items}

    def save_classification_draft(self, selections: list[dict], expected_revision: int) -> dict:
        # Reuse preview validation for shape/tenant IDs without committing any file changes.
        if selections:
            checked = self.classification_preview(selections)
            self.state.consume_preview(checked["preview_id"])
        return self.state.save_classification_draft(selections, expected_revision)

    def retry_classification(self, article_id: str) -> dict:
        article = next((item for item in self.articles() if item.get("article_id") == article_id), None)
        if not article:
            raise FileNotFoundError(article_id)
        full = self.read_article(article["path"])
        task = self.enqueue_classification(full)
        if not task:
            raise ValueError("classification requires a configured model")
        return task

    def classification_preview(self, selections: list[dict]) -> dict:
        if not isinstance(selections, list) or not selections or len(selections) > 500:
            raise ValueError("selections must contain 1 to 500 items")
        registry = dc.load_registry(self.root)
        planned_categories = list(registry["categories"])
        new_by_ref: dict[str, dict] = {}
        moves = []
        conflicts = []
        seen_article_ids: set[str] = set()
        seen_targets: dict[str, str] = {}
        article_by_id = {item["article_id"]: item for item in self.articles() if item.get("article_id")}
        for selection in selections:
            if not isinstance(selection, dict) or set(selection) - {"article_id", "article_revision", "category_id", "new_category", "tags", "decision"}:
                raise ValueError("invalid classification selection")
            article_id = selection.get("article_id")
            if not isinstance(article_id, str) or not article_id:
                raise ValueError("classification selection requires article_id")
            if article_id in seen_article_ids:
                conflicts.append({"article_id": article_id, "kind": "duplicate_article_selection"})
                continue
            seen_article_ids.add(article_id)
            if selection.get("decision") == "defer":
                continue
            article = article_by_id.get(article_id)
            if not article:
                raise FileNotFoundError(str(selection.get("article_id")))
            full = self.read_article(article["path"])
            if full["revision"] != selection.get("article_revision"):
                conflicts.append({"article_id": article["article_id"], "kind": "article_changed"})
                continue
            category_id = selection.get("category_id")
            new_spec = selection.get("new_category")
            if new_spec:
                if not isinstance(new_spec, dict) or set(new_spec) - {"client_ref", "name", "description"}:
                    raise ValueError("invalid new category")
                ref = str(new_spec.get("client_ref") or new_spec.get("name"))
                category = new_by_ref.get(ref)
                if not category:
                    category = dc.new_category(str(new_spec.get("name", "")), description=str(new_spec.get("description", "")), sort_order=len(planned_categories))
                    dc.assert_unique({"categories": planned_categories}, category)
                    planned_categories.append(category)
                    new_by_ref[ref] = category
                category_id = category["category_id"]
            try:
                category = dc.category_by_id({"categories": planned_categories}, str(category_id), include_archived=False)
            except FileNotFoundError:
                raise ValueError("invalid active category")
            target = f"{category['directory_name']}/{Path(article['path']).name}"
            target_key = dc.normalized_key(target)
            if target_key in seen_targets:
                conflicts.append({
                    "article_id": article["article_id"],
                    "other_article_id": seen_targets[target_key],
                    "kind": "duplicate_target_path",
                    "path": target,
                })
            else:
                seen_targets[target_key] = article["article_id"]
            target_path = self.root / "wiki" / target
            if target != article["path"] and target_path.exists():
                conflicts.append({"article_id": article["article_id"], "kind": "target_exists", "path": target})
            moves.append({
                "article_id": article["article_id"], "article_revision": full["revision"],
                "source_path": article["path"], "target_path": target, "category_id": category_id,
                "tags": dc.tags(full["markdown"]) if selection.get("tags") is None else [str(tag)[:50] for tag in selection.get("tags", [])][:20],
            })
        payload = {"taxonomy_revision": registry["revision"], "moves": moves, "new_categories": list(new_by_ref.values())}
        preview = self.state.create_preview("classification", payload)
        return {"preview_id": preview["preview_id"], "expires_at": preview["expires_at"], "moves": moves, "creates": list(new_by_ref.values()), "conflicts": conflicts, "can_commit": bool(moves) and not conflicts}

    def classification_commit(self, preview_id: str, *, clear_draft: bool = True) -> dict:
        preview = self.state.get_preview(preview_id, "classification")
        payload = preview["payload"]
        with self._intent_lock:
            registry = dc.load_registry(self.root)
            if registry["revision"] != payload["taxonomy_revision"]:
                raise RuntimeError("classification preview is stale")
            article_ids = [item["article_id"] for item in payload["moves"]]
            source_keys = [dc.normalized_key(item["source_path"]) for item in payload["moves"]]
            target_keys = [dc.normalized_key(item["target_path"]) for item in payload["moves"]]
            if len(article_ids) != len(set(article_ids)):
                raise RuntimeError("classification contains duplicate article selections")
            if len(source_keys) != len(set(source_keys)) or len(target_keys) != len(set(target_keys)):
                raise RuntimeError("classification contains duplicate paths")
            next_registry = json.loads(json.dumps(registry, ensure_ascii=False))
            for item in payload["new_categories"]:
                dc.assert_unique(next_registry, item)
                next_registry["categories"].append(item)
            taxonomy_changed = bool(payload["new_categories"])
            changes: dict[str, str | None] = {}
            if taxonomy_changed:
                next_registry["revision"] += 1
                changes[dc.REGISTRY_REL] = dc.dump_registry(next_registry)
            directories = {f"wiki/{item['directory_name']}": True for item in payload["new_categories"]}
            path_map: dict[str, str] = {}
            article_by_id = {item["article_id"]: item for item in self.articles() if item.get("article_id")}
            for move in payload["moves"]:
                article = article_by_id.get(move["article_id"])
                if not article:
                    raise FileNotFoundError(move["article_id"])
                current = self.read_article(article["path"])
                if current["revision"] != move["article_revision"] or current["path"] != move["source_path"]:
                    raise RuntimeError("article changed after preview")
                target = move["target_path"]
                if target != current["path"] and (self.root / "wiki" / target).exists():
                    raise FileExistsError(target)
                category = dc.category_by_id(next_registry, move["category_id"], include_archived=False)
                md = dc.ensure_article_metadata(current["markdown"], category_id=category["category_id"], status="confirmed", article_uuid=current["article_id"], article_tags=move["tags"])
                md = cats.ensure_category_header(md, category["directory_name"])
                md = wiki_ops.rebase_wiki_hrefs(md, current["path"], target)
                changes[f"wiki/{target}"] = md
                if target != current["path"]:
                    changes[f"wiki/{current['path']}"] = None
                    path_map[current["path"]] = target
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
            operation_id = f"classify-{uuid.uuid4().hex}"
            log_path = self.root / "wiki" / "log.md"
            changes["wiki/index.md"] = render_index(self.root, changes)
            changes["wiki/log.md"] = append_log_text(log_path.read_text(encoding="utf-8") if log_path.exists() else "", operation_id=operation_id, kind="classify", title=f"{len(payload['moves'])} articles")
            self.files.commit(changes, directories=directories, kind="classification", metadata={"path_map": path_map, "article_ids": [item["article_id"] for item in payload["moves"]]}, operation_id=operation_id)
            self._remap_committed_paths(operation_id, path_map)
            self.state.consume_preview(preview_id)
            if clear_draft:
                self.state.clear_classification_draft()
            return {
                "operation_id": operation_id,
                "moved_articles": [self.read_article(move["target_path"]) for move in payload["moves"]],
                "created_categories": payload["new_categories"],
                "path_map": path_map,
            }

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
        if action not in {"create", "rename", "archive", "restore", "delete", "reorder", "migrate"}:
            raise ValueError("invalid category action")
        registry = dc.load_registry(self.root)
        conflicts = []
        before = None
        after = None
        affected: list[str] = []
        if action == "create":
            after = dc.new_category(name, description=description, sort_order=len(registry["categories"]))
            dc.assert_unique(registry, after)
        else:
            before = dict(dc.category_by_id(registry, category_id))
            after = dict(before)
            folder = self.root / "wiki" / before["directory_name"]
            affected = [item["path"] for item in self.articles() if item["primary_category_id"] == category_id]
            if action == "migrate":
                target_category = dc.category_by_id(registry, target_category_id, include_archived=False)
                if target_category["category_id"] == before["category_id"]:
                    conflicts.append({"kind": "same_category"})
                if not affected:
                    conflicts.append({"kind": "category_empty"})
                nested = None
                if not conflicts:
                    selections = []
                    for article in self.articles():
                        if article["primary_category_id"] != before["category_id"]:
                            continue
                        full = self.read_article(article["path"])
                        selections.append({
                            "article_id": article["article_id"],
                            "article_revision": full["revision"],
                            "decision": "existing",
                            "category_id": target_category["category_id"],
                            "tags": article["tags"],
                        })
                    nested = self.classification_preview(selections)
                    conflicts.extend(nested["conflicts"])
                payload = {
                    "taxonomy_revision": registry["revision"],
                    "action": action,
                    "category_id": category_id,
                    "target_category_id": target_category_id,
                    "before": before,
                    "after": target_category,
                    "classification_preview_id": nested["preview_id"] if nested else None,
                }
                stored = self.state.create_preview("category", payload)
                return {
                    "preview_id": stored["preview_id"],
                    "expires_at": stored["expires_at"],
                    "action": action,
                    "before": before,
                    "after": target_category,
                    "affected_articles": affected,
                    "moves": nested["moves"] if nested else [],
                    "conflicts": conflicts,
                    "can_commit": bool(nested) and not conflicts,
                }
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
            registry = dc.load_registry(self.root)
            if registry["revision"] != payload["taxonomy_revision"]:
                raise RuntimeError("category preview is stale")
            action = payload["action"]
            before, after = payload.get("before"), payload.get("after")
            if action == "migrate":
                source = dc.category_by_id(registry, payload["category_id"])
                target = dc.category_by_id(registry, payload["target_category_id"], include_archived=False)
                if source != before or target != after:
                    raise RuntimeError("category changed after preview")
                nested_preview_id = payload.get("classification_preview_id")
                if not nested_preview_id:
                    raise RuntimeError("migration preview is incomplete")
                result = self.classification_commit(nested_preview_id, clear_draft=False)
                self.state.consume_preview(preview_id)
                return {**result, "category": target}
            next_registry = json.loads(json.dumps(registry, ensure_ascii=False))
            if action == "create":
                dc.assert_unique(next_registry, after)
                next_registry["categories"].append(after)
            else:
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
            if action == "create":
                directories[f"wiki/{after['directory_name']}"] = True
            elif action == "delete":
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
            self.files.commit(changes, directories=directories, kind=f"category-{action}", metadata={"path_map": path_map, "category_id": (after or before)["category_id"]}, operation_id=operation_id)
            self._remap_committed_paths(operation_id, path_map)
            self.state.consume_preview(preview_id)
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
            return self._reconciliation_commit_locked(preview_id)

    def _reconciliation_commit_locked(self, preview_id: str) -> dict:
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
        detected = item["payload"]
        if decision == "defer":
            result = self.state.resolve_reconciliation(item["id"], "deferred")
            self.state.consume_preview(preview_id)
            return {"operation_id": None, "item": result}
        registry = dc.load_registry(self.root)
        next_registry = json.loads(json.dumps(registry, ensure_ascii=False))
        changes: dict[str, str | None] = {}
        directories: dict[str, bool] = {}
        path_map: dict[str, str] = {}
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
        self.files.commit(changes, directories=directories, kind="reconciliation", metadata={"path_map": path_map, "reconciliation_id": item["id"]}, operation_id=operation_id)
        self._remap_committed_paths(operation_id, path_map)
        result = self.state.resolve_reconciliation(item["id"], "adopted" if decision == "adopt" else "restored")
        self.state.consume_preview(preview_id)
        return {"operation_id": operation_id, "item": result}

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
                derived_status = "queued" if current_task["status"] == "queued" else "failed"
                payload = task["payload"]
                if task["kind"] == "article-classification":
                    self.state.save_classification_suggestion(
                        payload["article_id"], payload["article_revision"], payload["taxonomy_revision"],
                        derived_status, task_id=task["id"], error_type=error_type,
                        error_message=error_message,
                    )
                elif task["kind"] == "raw-classification-plan":
                    self.state.save_raw_classification_plan(
                        payload["raw_path"], payload["raw_revision"], payload["taxonomy_revision"],
                        derived_status, task_id=task["id"], error_type=error_type,
                        error_message=error_message,
                    )
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
            if current_task["status"] == "failed" and current_task["attempts"] == task["attempts"]:
                if task["kind"] == "article-classification":
                    self.state.save_classification_suggestion(
                        task["payload"]["article_id"], task["payload"]["article_revision"],
                        task["payload"]["taxonomy_revision"], "failed", task_id=task["id"],
                        error_type="auth_revoked", error_message="The task initiator no longer has write access.",
                    )
                elif task["kind"] == "raw-classification-plan":
                    self.state.save_raw_classification_plan(
                        task["payload"]["raw_path"], task["payload"]["raw_revision"],
                        task["payload"]["taxonomy_revision"], "failed", task_id=task["id"],
                        error_type="auth_revoked", error_message="The task initiator no longer has write access.",
                    )
            return current_task
        payload = task["payload"]
        stale = bool(result.get("stale"))
        if not stale and task["kind"] == "article-classification":
            try:
                article = self.read_article(payload["path"])
                registry = dc.load_registry(self.root)
                stale = (
                    article["article_id"] != payload["article_id"]
                    or article["revision"] != payload["article_revision"]
                    or article["classification_status"] != "pending"
                    or registry["revision"] != payload["taxonomy_revision"]
                )
            except (FileNotFoundError, ValueError):
                stale = True
        elif not stale and task["kind"] == "raw-classification-plan":
            try:
                raw = self.read_raw(payload["raw_path"])
                registry = dc.load_registry(self.root)
                stale = raw["revision"] != payload["raw_revision"] or registry["revision"] != payload["taxonomy_revision"]
            except (FileNotFoundError, ValueError):
                stale = True
        if stale:
            message = "The source or category system changed while the task was running."
            current_task = self.state.fail_task(
                task["id"], "stale_revision", message, retry=False, expected_attempt=task["attempts"],
            )
            if current_task["status"] != "failed" or current_task["attempts"] != task["attempts"]:
                return current_task
            if task["kind"] == "article-classification":
                self.state.save_classification_suggestion(
                    payload["article_id"], payload["article_revision"], payload["taxonomy_revision"], "failed",
                    task_id=task["id"], error_type="stale_revision", error_message=message,
                )
            elif task["kind"] == "raw-classification-plan":
                self.state.save_raw_classification_plan(
                    payload["raw_path"], payload["raw_revision"], payload["taxonomy_revision"], "failed",
                    task_id=task["id"], error_type="stale_revision", error_message=message,
                )
            return current_task
        if result.get("cancelled"):
            return self.state.cancel_task(task["id"])
        if task["kind"] == "article-classification":
            return self.state.finalize_classification_success(
                task["id"], task["attempts"], result,
                article_id=payload["article_id"], article_revision=payload["article_revision"],
                taxonomy_revision=payload["taxonomy_revision"], suggestion=result["suggestion"],
            )
        if task["kind"] == "raw-classification-plan":
            return self.state.finalize_raw_classification_success(
                task["id"], task["attempts"], result,
                raw_path=payload["raw_path"], raw_revision=payload["raw_revision"],
                taxonomy_revision=payload["taxonomy_revision"], plan=result["plan"],
            )
        return self.state.complete_task(task["id"], result, expected_attempt=task["attempts"])

    def _run_remote_task(self, task: dict) -> dict:
        if task["kind"] == "raw-classification-plan":
            return self._run_raw_classification_plan(task)
        if task["kind"] == "article-classification":
            return self._run_classification_task(task)
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
        self.files.commit(changes, kind="remote-complete", metadata={"task": task["id"], "path": payload["path"]}, operation_id=operation_id)
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
        completed = self.read_article(payload["path"])
        if completed.get("classification_status") == "pending":
            self.enqueue_classification(completed, actor_user_id=task.get("actor_user_id"))
        return result

    def _run_raw_classification_plan(self, task: dict) -> dict:
        payload = task["payload"]
        if self.state.get_task(task["id"])["status"] == "cancelled":
            return {"cancelled": True, "raw_path": payload["raw_path"]}
        raw = self.read_raw(payload["raw_path"])
        registry = dc.load_registry(self.root)
        if raw["revision"] != payload["raw_revision"] or registry["revision"] != payload["taxonomy_revision"]:
            return {"stale": True, "raw_path": payload["raw_path"]}
        plan = self._call_raw_plan_llm(raw, registry)
        with self._intent_lock:
            if self.state.get_task(task["id"])["status"] == "cancelled":
                return {"cancelled": True, "raw_path": payload["raw_path"]}
            latest_raw = self.read_raw(payload["raw_path"])
            latest_registry = dc.load_registry(self.root)
            if latest_raw["revision"] != payload["raw_revision"] or latest_registry["revision"] != payload["taxonomy_revision"]:
                return {"stale": True, "raw_path": payload["raw_path"]}
        return {"stale": False, "raw_path": payload["raw_path"], "plan": plan}

    def _call_raw_plan_llm(self, raw: dict, registry: dict) -> dict:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RemoteError("not_configured", "The OpenAI-compatible client is not installed.") from exc
        active = [{"category_id": item["category_id"], "name": item["name"], "description": item["description"]} for item in registry["categories"] if item["status"] == "active"]
        prompt = (
            "阅读原料全文，给出预计分类方案。只输出 JSON object，含 candidates 数组（最多3项），每项包含 name、description、"
            "existing_category_id(null或已有ID)、confidence(0到1)、reason、expected_articles(标题数组)、expected_count。"
            "这只是预览，不得声称已创建分类。\n\n"
            f"已有分类：{json.dumps(active, ensure_ascii=False)}\n\n原料：\n{raw['markdown'][:50000]}"
        )
        client = OpenAI(api_key=self.llm.api_key or "not-needed", base_url=self.llm.base_url, timeout=30, max_retries=0)
        response = client.chat.completions.create(model=self.llm.model, messages=[{"role": "system", "content": "你是知识规划器，只返回严格 JSON。"}, {"role": "user", "content": prompt}], max_tokens=1800)
        text = model_message_text(response.choices[0].message)
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S | re.I)
        data = json.loads(fenced.group(1) if fenced else text)
        candidates = data.get("candidates") if isinstance(data, dict) else None
        valid_ids = {item["category_id"] for item in active}
        if not isinstance(candidates, list) or len(candidates) > 3:
            raise RemoteError("model_error", "原料分类方案结构无效。", retryable=True)
        clean = []
        for item in candidates:
            if not isinstance(item, dict) or item.get("existing_category_id") not in valid_ids | {None}:
                raise RemoteError("model_error", "原料分类候选无效。", retryable=True)
            confidence = float(item.get("confidence", -1))
            if not 0 <= confidence <= 1:
                raise RemoteError("model_error", "原料分类置信度无效。", retryable=True)
            clean.append({"name": dc.validate_name(str(item.get("name", ""))), "description": str(item.get("description", ""))[:500], "existing_category_id": item.get("existing_category_id"), "confidence": confidence, "reason": str(item.get("reason", ""))[:300], "expected_articles": [str(value)[:120] for value in item.get("expected_articles", []) if isinstance(value, str)][:20], "expected_count": max(0, min(1000, int(item.get("expected_count", 0))))})
        return {"candidates": clean}

    def _run_classification_task(self, task: dict) -> dict:
        payload = task["payload"]
        if self.state.get_task(task["id"])["status"] == "cancelled":
            return {"cancelled": True, "article_id": payload["article_id"]}
        article = self.read_article(payload["path"])
        registry = dc.load_registry(self.root)
        if article["article_id"] != payload["article_id"] or article["revision"] != payload["article_revision"] or registry["revision"] != payload["taxonomy_revision"] or article["classification_status"] != "pending":
            return {"stale": True, "article_id": payload["article_id"]}
        suggestion = self._call_classification_llm(article, registry)
        with self._intent_lock:
            if self.state.get_task(task["id"])["status"] == "cancelled":
                return {"cancelled": True, "article_id": payload["article_id"]}
            latest = self.read_article(payload["path"])
            latest_registry = dc.load_registry(self.root)
            if (
                latest["article_id"] != payload["article_id"]
                or latest["revision"] != payload["article_revision"]
                or latest["classification_status"] != "pending"
                or latest_registry["revision"] != payload["taxonomy_revision"]
            ):
                return {"stale": True, "article_id": payload["article_id"]}
        return {"stale": False, "article_id": article["article_id"], "suggestion": suggestion}

    def _call_classification_llm(self, article: dict, registry: dict) -> dict:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RemoteError("not_configured", "The OpenAI-compatible client is not installed.") from exc
        active = [
            {key: item.get(key) for key in ("category_id", "name", "description", "aliases")}
            for item in registry["categories"] if item["status"] == "active"
        ]
        prompt = (
            "根据完整 Markdown 正文和用户已有分类给出归类建议。只输出 JSON object："
            "candidates 数组最多3项，每项 category_id、confidence(0到1)、reason；tags 字符串数组；"
            "new_category 可为 null 或包含 name、description、confidence、reason。不得发明 category_id。\n\n"
            f"已有分类：{json.dumps(active, ensure_ascii=False)}\n\n正文：\n{article['markdown'][:50000]}"
        )
        client = OpenAI(api_key=self.llm.api_key or "not-needed", base_url=self.llm.base_url, timeout=30, max_retries=0)
        response = client.chat.completions.create(
            model=self.llm.model,
            messages=[{"role": "system", "content": "你是知识归类器，只返回严格 JSON。"}, {"role": "user", "content": prompt}],
            max_tokens=1600,
        )
        raw = model_message_text(response.choices[0].message)
        fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S | re.I)
        data = json.loads(fenced.group(1) if fenced else raw)
        if not isinstance(data, dict) or set(data) - {"candidates", "tags", "new_category"}:
            raise RemoteError("model_error", "分类模型返回了无效结构。", retryable=True)
        valid_ids = {item["category_id"] for item in active}
        candidates = data.get("candidates", [])
        if not isinstance(candidates, list) or len(candidates) > 3:
            raise RemoteError("model_error", "分类候选数量无效。", retryable=True)
        clean_candidates = []
        for item in candidates:
            if not isinstance(item, dict) or item.get("category_id") not in valid_ids:
                raise RemoteError("model_error", "分类候选不属于当前空间。", retryable=True)
            confidence = float(item.get("confidence", -1))
            if not 0 <= confidence <= 1:
                raise RemoteError("model_error", "分类置信度无效。", retryable=True)
            clean_candidates.append({"category_id": item["category_id"], "confidence": confidence, "reason": str(item.get("reason", ""))[:300]})
        tags = dc.tags("> Tags: " + "; ".join(str(tag) for tag in data.get("tags", []) if isinstance(tag, str)))
        new_category = data.get("new_category")
        if new_category is not None:
            if not isinstance(new_category, dict):
                raise RemoteError("model_error", "新分类建议无效。", retryable=True)
            name = dc.validate_name(str(new_category.get("name", "")))
            new_category = {"name": name, "description": str(new_category.get("description", ""))[:500], "confidence": max(0.0, min(1.0, float(new_category.get("confidence", 0)))), "reason": str(new_category.get("reason", ""))[:300]}
        return {"candidates": clean_candidates, "tags": tags, "new_category": new_category}

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
