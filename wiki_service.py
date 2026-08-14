"""Application service for reading, governing, and generating the Markdown wiki."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Callable

import aliases
import categories as cats
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
    grouped: dict[str, list[tuple[str, str, str]]] = {cid: [] for cid in cats.ORDER}
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
        category = cats.category_for_article(md, wiki_rel)
        grouped.setdefault(category, []).append((wiki_rel, title, article_summary(md)))
    lines = ["# Knowledge Base Index", ""]
    for category in cats.ORDER:
        rows = grouped.get(category, [])
        if not rows:
            continue
        lines.extend([f"## {category}", "", cats.blurb_of(category), "", "| Article | Summary | Updated |", "|---------|---------|---------|"])
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
    ):
        self.root = project_root.resolve()
        (self.root / "wiki").mkdir(exist_ok=True)
        (self.root / "raw").mkdir(exist_ok=True)
        self.files = FileStore(self.root)
        self.state = StateStore(self.root)
        self.llm = llm_config or LLMConfig()
        self.remote_search = remote_search or websearch.search_sources
        self.remote_task_kinds = remote_task_kinds
        self._stop = threading.Event()
        self._intent_lock = threading.RLock()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        if start_worker:
            self._worker = threading.Thread(target=self._worker_loop, name="wiki-remote-worker", daemon=True)
            self._worker.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._worker:
            self._worker.join(timeout=3)

    def configure_llm(self, config: LLMConfig) -> None:
        with self._intent_lock:
            self.llm = config
        self._wake.set()

    def categories(self) -> list[dict]:
        return [{"id": cid, "label": label, "blurb": blurb} for cid, label, blurb, _ in cats.CATALOG]

    def articles(self) -> list[dict]:
        rows = []
        for path in kw.iter_articles(self.root):
            rel = kw.rel_article(self.root, path)
            md = path.read_text(encoding="utf-8")
            category = cats.category_for_article(md, rel)
            rows.append(
                {
                    "path": rel,
                    "title": kw.parse_title(md) or path.stem,
                    "aliases": aliases.parse_aliases(md),
                    "category": category,
                    "category_label": cats.label_of(category),
                    "content_status": wiki_ops.parse_status(md) or "词条",
                    "completeness": wiki_ops.structure_completeness(md),
                    "evidence_status": wiki_ops.parse_evidence_status(md),
                }
            )
        rows.sort(key=lambda row: (cats.ORDER.index(row["category"]) if row["category"] in cats.ORDER else 99, row["title"].casefold()))
        return rows

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
        category = cats.category_for_article(md, final)
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
            "category_label": cats.label_of(category),
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
        category = cats.classify(term, from_path, heading, passage)
        return {
            "keyword": term,
            "existing_path": existing,
            "category": category,
            "category_label": cats.label_of(category),
            "local_coverage": evidence,
            "context": {"from_path": from_path, "heading": heading, "passage": passage[:800]},
            "excerpts": excerpts,
            "plan": "open_existing" if existing else ("local_generate" if evidence["sufficient"] else "local_draft_then_remote_supplement"),
        }

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
        category = preflight["category"]
        rel = f"{category}/{slugify(term)}.md"
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
            task, _created = self.state.enqueue_task(task_kind, term, task_payload, staged=True)
        evidence_status = "待补证" if needs_remote else "本地已核验"
        md = self._draft_markdown(term, preflight["excerpts"], rel, category, task_id=task["id"] if task else None, evidence_status=evidence_status)
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
        return {"created": True, "task": task, "article": self.read_article(rel), "operation_id": manifest["operation_id"], "preflight": preflight}

    def apply_meta(self, rel: str, *, category: str, status: str) -> dict:
        if category not in cats.BY_ID:
            raise ValueError("invalid category")
        if status not in STATUS_VALUES:
            raise ValueError("invalid status")
        article = self.read_article(rel)
        old_rel = article["path"]
        md = cats.ensure_category_header(article["markdown"], category)
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
        operation_id = f"meta-{hashlib.sha256((old_rel + target_rel + revision(md)).encode()).hexdigest()[:20]}"
        log_path = self.root / "wiki" / "log.md"
        changes["wiki/index.md"] = render_index(self.root, changes)
        changes["wiki/log.md"] = append_log_text(log_path.read_text(encoding="utf-8") if log_path.exists() else "", operation_id=operation_id, kind="govern", title=article["title"])
        self.files.commit(changes, kind="meta", metadata={"source": old_rel, "target": target_rel}, operation_id=operation_id)
        updated = self.read_article(target_rel)
        self.state.remap_article_path(old_rel, target_rel, base_revision=updated["revision"])
        return {"operation_id": operation_id, "article": updated}

    def save_article(self, rel: str, markdown: str, expected_revision: str, *, force: bool = False) -> dict:
        current = self.read_article(rel)
        if not force and current["revision"] != expected_revision:
            return {"conflict": True, "disk": current}
        title = kw.parse_title(markdown)
        if not title:
            raise ValueError("article must contain one H1 title")
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
        category = cats.classify(title, raw_path, "", raw["markdown"][:800])
        suggestion = "duplicate" if item["status"] in {"duplicate", "ingested"} else ("supplement" if existing else ("seed" if not self.articles() else "new"))
        return {"raw": item, "markdown": raw["markdown"], "suggested_title": title, "suggested_category": category, "suggested_disposition": suggestion, "suggested_target": existing or item.get("linked_target"), "preview_changes": ["Sources", "Raw", "index", "log"]}

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

    def ingest_commit(self, raw_path: str, disposition: str, *, title: str, category: str, target_path: str | None = None) -> dict:
        if disposition not in {"seed", "new", "supplement", "duplicate", "defer"}:
            raise ValueError("invalid disposition")
        preview = self.ingest_preview(raw_path)
        item = preview["raw"]
        if item["status"] == "integrity_changed":
            raise ValueError("Raw file changed after it was recorded; save it as a new version")
        if category not in cats.BY_ID:
            raise ValueError("invalid category")
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
            result_target = self.read_article(existing_target)["path"] if existing_target else f"{category}/{slugify(title)}.md"
            raw_link = source_link(result_target, raw_path, raw["title"])
            changes[f"wiki/{result_target}"] = self._seed_markdown(raw["markdown"], category=category, raw_link=raw_link)
        elif disposition == "new":
            result_target = f"{category}/{slugify(title)}.md"
            if aliases.resolve(self.root, title) or (self.root / "wiki" / result_target).exists():
                raise ValueError("canonical article already exists; use supplement")
            excerpt = [{"title": raw["title"], "path": raw_path, "text": raw["markdown"][:900]}]
            md = self._draft_markdown(title, excerpt, result_target, category, task_id=None, evidence_status="Raw 已核验")
            changes[f"wiki/{result_target}"] = md
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
        return self.files.rollback(operation_id)

    def retry_task(self, task_id: str) -> dict:
        task = self.state.get_task(task_id)
        payload = dict(task["payload"])
        if payload.get("path"):
            payload["base_revision"] = self.read_article(str(payload["path"]))["revision"]
        task = self.state.retry_task(task_id, payload=payload)
        self._wake.set()
        return task

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
            )
            tasks.append(task)
            queued += int(created)
        if queued:
            self._wake.set()
        return {"queued": queued, "tasks": tasks}

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            task = self.state.claim_task(self.remote_task_kinds)
            if not task:
                self._wake.wait(1)
                self._wake.clear()
                continue
            try:
                result = self._run_remote_task(task)
                if self.state.get_task(task["id"])["status"] == "running":
                    self.state.complete_task(task["id"], result)
            except RemoteError as exc:
                self.state.fail_task(task["id"], exc.code, str(exc), retry=exc.retryable)
            except Exception as exc:
                self.state.fail_task(task["id"], "model_error", str(exc), retry=False)

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
        quality = wiki_ops.article_quality_issues(self.root, payload["path"], proposal, extra_paths=set(raw_changes))
        if quality:
            raise RemoteError("quality_gate", "AI output failed quality checks: " + "; ".join(quality), retryable=False)
        latest = self.read_article(payload["path"])
        if latest["revision"] != base_revision:
            return {"conflict": True, "path": payload["path"], "proposal": proposal, "reason": "article_changed"}
        if self.state.get_task(task["id"])["status"] == "cancelled":
            return {"cancelled": True, "path": payload["path"]}
        operation_id = f"task-{task['id']}"
        log_path = self.root / "wiki" / "log.md"
        changes = {**raw_changes, f"wiki/{payload['path']}": proposal, "wiki/index.md": render_index(self.root, {f"wiki/{payload['path']}": proposal})}
        changes["wiki/log.md"] = append_log_text(log_path.read_text(encoding="utf-8") if log_path.exists() else "", operation_id=operation_id, kind="remote-complete", title=payload["keyword"])
        self.files.commit(changes, kind="remote-complete", metadata={"task": task["id"], "path": payload["path"]}, operation_id=operation_id)
        return {"conflict": False, "path": payload["path"], "operation_id": operation_id, "raw_paths": list(raw_changes)}

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
        latest = self.read_article(current["path"])
        if latest["revision"] != payload["base_revision"]:
            return {"conflict": True, "path": current["path"], "proposal": proposal, "reason": "article_changed"}
        operation_id = f"govern-{task['id']}"
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
        self.files.commit(changes, kind="ai-govern", metadata={"task": task["id"], "path": current["path"]}, operation_id=operation_id)
        return {"conflict": False, "path": current["path"], "operation_id": operation_id}

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
