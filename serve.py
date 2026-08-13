#!/usr/bin/env python3
"""Local wiki viewer: highlight keywords, generate explanation pages on click."""

from __future__ import annotations

import json
import os
import re
import traceback
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import aliases
import categories as cats
import keywords as kw
import websearch
import wiki_ops

ROOT = Path(__file__).resolve().parent
VIEWER = ROOT / "viewer" / "index.html"
CONCEPTS_DIR = ROOT / "wiki" / "concepts"
HOST = "127.0.0.1"
PORT = 8765


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def llm_base_url() -> str:
    return env("LLM_BASE_URL").rstrip("/")


def llm_api_key() -> str:
    return env("LLM_API_KEY") or env("OPENAI_API_KEY")


def llm_model() -> str:
    return env("LLM_MODEL")


def llm_configured() -> bool:
    return bool(llm_base_url() and llm_model())


def slugify(term: str) -> str:
    text = term.strip()
    text = re.sub(r"[\\/:*?\"<>|]+", "", text)
    text = re.sub(r"\s+", "-", text)
    text = text.strip("-.") or "keyword"
    return text[:60]


def article_list() -> list[dict]:
    rows = []
    for path in kw.iter_articles(ROOT):
        rel = kw.rel_article(ROOT, path)
        md = path.read_text(encoding="utf-8")
        title = kw.parse_title(md) or path.stem
        category = cats.category_for_article(md, rel)
        rows.append(
            {
                "path": rel,
                "title": title,
                "topic": category,
                "category": category,
                "category_label": cats.label_of(category),
                "category_blurb": cats.blurb_of(category),
                "completeness": wiki_ops.completeness(md),
                "status": wiki_ops.parse_status(md),
            }
        )
    rows.sort(key=lambda r: (cats.ORDER.index(r["category"]) if r["category"] in cats.ORDER else 99, r["title"]))
    return rows


def read_raw(rel: str) -> dict:
    rel = (rel or "").strip()
    if not rel.startswith("raw/") or ".." in rel or rel.startswith("/"):
        raise ValueError("invalid path")
    raw_root = (ROOT / "raw").resolve()
    path = (ROOT / rel).resolve()
    if not path.is_relative_to(raw_root) or not path.is_file():
        raise FileNotFoundError(rel)
    md = path.read_text(encoding="utf-8")
    return {
        "path": rel,
        "title": kw.parse_title(md) or path.stem,
        "markdown": md,
        "kind": "raw",
    }


def read_article(rel: str) -> dict:
    if not rel or ".." in rel or rel.startswith("/"):
        raise ValueError("invalid path")
    path = (ROOT / "wiki" / rel).resolve()
    wiki = (ROOT / "wiki").resolve()
    if not str(path).startswith(str(wiki)) or not path.is_file():
        raise FileNotFoundError(rel)
    md = path.read_text(encoding="utf-8")
    category = cats.category_for_article(md, rel)
    return {
        "path": rel,
        "title": kw.parse_title(md) or path.stem,
        "markdown": md,
        "category": category,
        "category_label": cats.label_of(category),
        "completeness": wiki_ops.completeness(md),
        "status": wiki_ops.parse_status(md),
        "backlinks": wiki_ops.backlinks(ROOT, rel),
    }


def existing_path_for(term: str) -> str | None:
    return aliases.resolve(ROOT, term)


def relative_source_link(from_rel: str, to_rel: str, title: str) -> str:
    from_abs = Path("wiki") / Path(from_rel).parent
    to_abs = Path(to_rel) if to_rel.startswith("raw/") else Path("wiki") / to_rel
    target = Path(os.path.relpath(to_abs, from_abs)).as_posix()
    return f"[{title}]({target})"


def search_query(term: str, heading: str, passage: str) -> str:
    parts = [term]
    if heading and heading not in {term, "Overview", "解释"}:
        parts.append(heading[:24])
    if passage:
        for en in re.findall(r"[A-Za-z][A-Za-z0-9]+(?:[ \-][A-Za-z][A-Za-z0-9]+)+", passage):
            if en.lower() != term.lower() and en not in parts:
                parts.append(en)
                break
        idx = passage.find(term)
        if idx >= 0:
            window = passage[max(0, idx - 18) : idx + len(term) + 18]
            window = re.sub(r"\s+", " ", window).strip()
            if window and window != term and window not in parts:
                parts.append(window)
    # keep query compact so search stays on-sense
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return " ".join(seen)[:80]


def context_needles(heading: str, passage: str) -> list[str]:
    out = []
    if heading and 2 <= len(heading) <= 24:
        out.append(heading)
    return out


def extractive_article(term: str, excerpts: list[dict], dest_rel: str, category: str = "concepts") -> str:
    today = date.today().isoformat()
    if excerpts:
        sources = "; ".join(
            relative_source_link(dest_rel, item["path"], item["title"]) for item in excerpts
        )
    else:
        sources = "（本 wiki 中暂无直接出处）"
    blocks = []
    for item in excerpts:
        link = relative_source_link(dest_rel, item["path"], item["title"])
        blocks.append(f"### {item['title']}\n\n来自 {link}：\n\n> {item['text']}\n")
    lead = ""
    if excerpts:
        lead = excerpts[0]["text"].strip()
        lead = re.sub(r"^[#>*\s]+", "", lead)
        if len(lead) > 220:
            lead = lead[:217].rstrip() + "…"
    else:
        lead = f"{term}。"
    body = "\n".join(blocks) if blocks else ""
    return (
        f"# {term}\n\n"
        f"> Category: {category}\n"
        f"> Status: 草稿\n"
        f"> Sources: {sources}\n"
        f"> Archived: {today}\n\n"
        f"## Overview\n\n"
        f"{lead}\n\n"
        f"## 它做什么\n\n"
        f"{lead}\n\n"
        f"## 怎么用\n\n"
        f"结合已有条目中的说法使用「{term}」，先对齐场合再套用。\n\n"
        f"## 例子\n\n"
        f"1. 在原文出现该词的场合直接套用其义项。\n"
        f"2. 不要把它换成另一个常见但此处未出现的含义。\n\n"
        + (f"## 依据\n\n{body}\n" if body else "")
        + f"## See Also\n\n"
        + "\n".join(f"- {relative_source_link(dest_rel, item['path'], item['title'])}" for item in excerpts)
        + ("\n" if excerpts else "")
    )


def llm_article(
    term: str,
    excerpts: list[dict],
    dest_rel: str,
    *,
    from_path: str = "",
    heading: str = "",
    passage: str = "",
    used_web: bool = False,
    category: str | None = None,
) -> str:
    from openai import OpenAI

    today = date.today().isoformat()
    packed = []
    for item in excerpts:
        packed.append(f"[{item['title']}]({item['path']}):\n{item['text']}")
    context = "\n\n".join(packed) if packed else "(no excerpts)"
    wiki_links = [item for item in excerpts if not item["path"].startswith("raw/")]
    source_line = (
        "; ".join(relative_source_link(dest_rel, item["path"], item["title"]) for item in wiki_links)
        or "（待补来源）"
    )
    raw_items = [item for item in excerpts if item["path"].startswith("raw/")]
    raw_line = "; ".join(f"[{item['title']}](../../{item['path']})" for item in raw_items)
    header_extra = f"\n> Raw: {raw_line}" if raw_line else ""
    date_field = f"> Updated: {today}" if used_web else f"> Archived: {today}"
    cat_id = category or cats.classify(term, from_path, heading, passage)
    cat_label = cats.label_of(cat_id)
    prompt = f"""写一篇可以长期放进知识库的中文词条，标题是「{term}」。
读起来要像正常百科 / 教材小节，把这个知识点讲细，不要写成摘要或问答回复。

先在心里根据出现场合确定义项（多义词只写这一个），但正文不要提「点击」「上下文」「摘录」「读者」「本页」「上述」。
禁止：在上述上下文中、点出来的关键词、现有摘录、文中。

出现场合（只用来选义项）：
来源：{from_path or "未知"}
小节：{heading or "（无）"}
段落：{passage or "（无）"}
归类：{cat_label}（{cat_id}）

可引用的资料：
{context}

规则：
- 优先用已有 wiki 事实；不够再用 raw/。
- 不要编造数字、日期、产品名和引语；资料不够的部分写清「常见做法是…」而不要假装有出处。
- 每个小节都要有实质内容，不要一句话敷衍。
- 至少举 1 个正例和 1 个反例或边界（如果资料里有的话用资料里的；没有就用该义项下最典型、且不涉及未给出数字的例子）。
- 用中文。只输出 markdown。

格式必须包含这些标题（可在其后加更细的小标题）：
# {term}

> Category: {cat_id}
> Sources: {source_line}{header_extra}
{date_field}

## Overview
3-5 句：它是什么、解决什么问题、和相邻概念差在哪。

## 它做什么
把机制拆开写：输入是什么、中间怎么处理、输出是什么。

## 怎么用
给出可照着做的步骤或句式。有公式或操作句就原样写出。

## 适用与不适用
分条写什么时候该用、什么时候不该用。

## 例子
至少两个具体场景，写清怎么说、会得到什么不同。

## 注意点
误区、限制、隐私或时效（有才写，没有就省略本节）。

## See Also
只用 Sources 里已有的相对链接
"""
    client = OpenAI(
        api_key=llm_api_key() or "not-needed",
        base_url=llm_base_url(),
    )
    resp = client.chat.completions.create(
        model=llm_model(),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4096,
    )
    text = ((resp.choices[0].message.content) or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:markdown|md)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    if not text.startswith("#"):
        raise RuntimeError("model did not return a wiki page")
    return polish_article(text) + ("\n" if not text.endswith("\n") else "")


META_LEAD = re.compile(
    r"^(在上述上下文中|在上述上下文里|在这段话里|在本文的语境中|在教程的语境里|"
    r"在教程的警告语境中|此处的|文中)[，,：:]?"
)


def polish_article(md: str) -> str:
    out = []
    for line in md.splitlines():
        stripped = line.lstrip()
        if stripped and not stripped.startswith(("#", ">", "-", "*", "|")):
            line = META_LEAD.sub("", line)
            line = re.sub(
                r"^「[^」]+」是从现有 wiki 条目里点出来的关键词。.*$",
                "",
                line,
            )
        out.append(line)
    text = "\n".join(out)
    text = re.sub(r"现有摘录[^\n]*", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def upsert_index(title: str, rel: str, summary: str, category: str = "concepts") -> None:
    index = ROOT / "wiki" / "index.md"
    text = index.read_text(encoding="utf-8") if index.exists() else "# Knowledge Base Index\n"
    link = f"[{title}]({rel})"
    if link in text:
        return
    section = f"## {category}"
    row = f"| {link} | {summary} | {date.today().isoformat()} |"
    if section not in text:
        text = text.rstrip() + (
            f"\n\n## {category}\n\n"
            f"{cats.blurb_of(category)}\n\n"
            "| Article | Summary | Updated |\n"
            "|---------|---------|---------|\n"
            f"{row}\n"
        )
    else:
        # append inside this section: before the next ## or at end
        parts = re.split(r"(?=\n## )", "\n" + text.lstrip("\n"))
        rebuilt = []
        for part in parts:
            if part.startswith(f"\n## {category}") or part.startswith(f"## {category}"):
                part = part.rstrip() + f"\n{row}\n"
            rebuilt.append(part)
        text = "".join(rebuilt).lstrip("\n")
        if link not in text:
            text = text.rstrip() + f"\n{row}\n"
    index.write_text(text, encoding="utf-8")


def append_log(title: str) -> None:
    log = ROOT / "wiki" / "log.md"
    if not log.exists():
        log.write_text("# Wiki Log\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## [{date.today().isoformat()}] query | Archived: {title}\n")


def generate_article(
    term: str,
    *,
    from_path: str = "",
    heading: str = "",
    passage: str = "",
) -> dict:
    term = kw.normalize(term)
    if not term or kw.is_skippable(term):
        raise ValueError("not a usable keyword")
    existing = existing_path_for(term)
    if existing:
        return {**read_article(existing), "created": False, "used_web": False}

    category = cats.classify(term, from_path, heading, passage)
    dest_dir = ROOT / "wiki" / category
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_rel = f"{category}/{slugify(term)}.md"
    dest = ROOT / "wiki" / dest_rel
    if dest.exists():
        return {**read_article(dest_rel), "created": False, "used_web": False}

    extras = context_needles(heading, passage)
    excerpts = kw.excerpts_for(ROOT, term, extra_needles=extras)
    used_web = False
    search_failed = False
    raw_paths: list[str] = []
    if wiki_ops.would_use_web(ROOT, term, extra_needles=extras):
        query = search_query(term, heading, passage)
        try:
            pages = websearch.search_sources(query, limit=3, keyword=term)
        except Exception:
            traceback.print_exc()
            pages = []
            search_failed = True
        if not pages:
            search_failed = True
        for page in pages:
            try:
                raw_rel = websearch.save_raw(ROOT, page)
            except OSError:
                search_failed = True
                continue
            raw_paths.append(raw_rel)
            excerpts.append({"title": page["title"], "path": raw_rel, "text": page["text"][:900]})
            used_web = True
        if used_web:
            search_failed = False

    if llm_configured():
        try:
            md = llm_article(
                term,
                excerpts,
                dest_rel,
                from_path=from_path,
                heading=heading,
                passage=passage,
                used_web=used_web,
                category=category,
            )
        except Exception:
            traceback.print_exc()
            md = extractive_article(term, excerpts, dest_rel, category=category)
    else:
        md = extractive_article(term, excerpts, dest_rel, category=category)

    md = cats.ensure_category_header(md, category)
    if wiki_ops.completeness(md) != "词条":
        md = wiki_ops.ensure_status_header(md, wiki_ops.parse_status(md) or "草稿")
    dest.write_text(md, encoding="utf-8")
    wiki_ops.update_referring_pages(ROOT, term, dest_rel, term)
    wiki_ops.rebuild_index(ROOT)
    if raw_paths:
        log = ROOT / "wiki" / "log.md"
        if not log.exists():
            log.write_text("# Wiki Log\n", encoding="utf-8")
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"\n## [{date.today().isoformat()}] ingest | {term}\n")
            fh.write("- Disposition: New\n")
            for raw_rel in raw_paths:
                fh.write(f"- Raw: {raw_rel}\n")
    else:
        append_log(term)
    return {
        **read_article(dest_rel),
        "created": True,
        "used_web": used_web,
        "search_failed": search_failed,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: object) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, raw, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path in {"/", "/index.html"}:
            self._send(200, VIEWER.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._json(
                200,
                {
                    "configured": llm_configured(),
                    "base_url": llm_base_url() or None,
                    "model": llm_model() or None,
                    "has_api_key": bool(llm_api_key()),
                },
            )
            return
        if path == "/api/articles":
            self._json(200, article_list())
            return
        if path == "/api/categories":
            self._json(
                200,
                [
                    {"id": cid, "label": label, "blurb": blurb}
                    for cid, label, blurb, _hints in cats.CATALOG
                ],
            )
            return
        if path == "/api/keywords":
            self._json(200, wiki_ops.highlightable_keywords(ROOT))
            return
        if path == "/api/todo":
            self._json(200, wiki_ops.mentioned_but_missing(ROOT))
            return
        if path == "/api/lint":
            self._json(200, wiki_ops.lint_wiki(ROOT))
            return
        if path == "/api/article":
            rel = parse_qs(parsed.query).get("path", [""])[0]
            try:
                self._json(200, read_article(rel))
            except FileNotFoundError:
                self._json(404, {"error": "not found"})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/api/raw":
            rel = parse_qs(parsed.query).get("path", [""])[0]
            try:
                self._json(200, read_raw(rel))
            except FileNotFoundError:
                self._json(404, {"error": "not found"})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._json(400, {"error": str(exc)})
            return
        try:
            if parsed.path == "/api/generate":
                term = str(data.get("keyword") or "").strip()
                self._json(
                    200,
                    generate_article(
                        term,
                        from_path=str(data.get("from_path") or "").strip(),
                        heading=str(data.get("heading") or "").strip(),
                        passage=str(data.get("passage") or "").strip()[:800],
                    ),
                )
                return
            if parsed.path == "/api/meta":
                rel = wiki_ops.safe_wiki_rel(str(data.get("path") or "").strip())
                category = str(data.get("category") or "").strip() or None
                status = data.get("status")
                if status is not None:
                    status = str(status).strip()
                new_rel = wiki_ops.apply_meta(ROOT, rel, category=category, status=status)
                wiki_ops.rebuild_index(ROOT)
                self._json(200, read_article(new_rel))
                return
            self._json(404, {"error": "not found"})
        except FileNotFoundError:
            self._json(404, {"error": "not found"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            self._json(500, {"error": str(exc)})


def main() -> None:
    load_dotenv()
    CONCEPTS_DIR.mkdir(parents=True, exist_ok=True)
    keep = CONCEPTS_DIR / ".gitkeep"
    if not keep.exists():
        keep.write_text("", encoding="utf-8")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Wiki viewer: http://{HOST}:{PORT}")
    print("Click a solid keyword to open its page. Click a dashed keyword to generate one.")
    if llm_configured():
        print(f"LLM: {llm_model()} @ {llm_base_url()}")
    else:
        print("No LLM_BASE_URL / LLM_MODEL: click-to-generate will compile from existing wiki text.")
        print("Set them in .env to use your own endpoint.")
    server.serve_forever()


if __name__ == "__main__":
    main()
