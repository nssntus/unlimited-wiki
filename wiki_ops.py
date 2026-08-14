"""Back-links, completeness, lint, and metadata writes over the wiki tree."""

from __future__ import annotations

import os
import re
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

import aliases
import categories as cats
import keywords as kw

REQUIRED_HEADINGS = ("它做什么", "机制", "怎么用", "例子")
STATUS_RE = re.compile(r"^>\s*Status:\s*(.+)$", re.M | re.I)
GENERATION_RE = re.compile(r"^>\s*Generation:\s*(.+)$", re.M | re.I)
EVIDENCE_RE = re.compile(r"^>\s*Evidence:\s*(.+)$", re.M | re.I)
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
WIKI_SKIP = {"index.md", "log.md"}
VALID_STATUSES = {"词条", "草稿", "过时", "有争议"}


def rel_href(from_rel: str, to_rel: str) -> str:
    from_abs = Path("wiki") / Path(from_rel).parent
    to_abs = Path(to_rel) if to_rel.startswith("raw/") else Path("wiki") / to_rel
    return Path(os.path.relpath(to_abs, from_abs)).as_posix()


def resolve_href(from_rel: str, href: str) -> str | None:
    if not href or href.startswith(("http://", "https://", "mailto:", "#")):
        return None
    href = href.split("#", 1)[0]
    if href.startswith("../../raw/") or href.startswith("raw/"):
        return href.replace("../../", "")
    parts = list(Path(from_rel).parent.parts)
    for piece in Path(href).parts:
        if piece == "..":
            if parts:
                parts.pop()
        elif piece != ".":
            parts.append(piece)
    return "/".join(parts) if parts else None


def parse_status(md: str) -> str | None:
    m = STATUS_RE.search(md or "")
    if not m:
        return None
    value = m.group(1).strip()
    return value or None


def ensure_status_header(md: str, status: str) -> str:
    if not status:
        return STATUS_RE.sub("", md, count=1)
    line = f"> Status: {status}"
    if STATUS_RE.search(md):
        return STATUS_RE.sub(line, md, count=1)
    lines = md.splitlines()
    insert_at = 0
    for i, row in enumerate(lines):
        if row.startswith("# "):
            insert_at = i + 1
            while insert_at < len(lines) and lines[insert_at] == "":
                insert_at += 1
            break
    lines.insert(insert_at, line)
    return "\n".join(lines) + ("\n" if md.endswith("\n") else "")


def has_heading(md: str, title: str) -> bool:
    return bool(re.search(rf"^##\s+{re.escape(title)}\s*$", md, re.M))


def completeness(md: str) -> str:
    status = parse_status(md)
    if status in {"过时", "有争议"}:
        return status
    mech = has_heading(md, "它做什么") or has_heading(md, "机制")
    how = has_heading(md, "怎么用")
    examples = has_heading(md, "例子")
    if mech and how and examples:
        return "词条"
    heads = re.findall(r"^##\s+(.+)$", md, re.M)
    if len(heads) >= 3 and len(md) >= 800:
        return "词条"
    if status == "草稿":
        return "草稿"
    return "草稿"


def structure_completeness(md: str) -> str:
    if (parse_generation(md) or "").startswith("seed-adopted"):
        return "完整"
    mech = has_heading(md, "它做什么") or has_heading(md, "机制")
    missing = []
    if not mech:
        missing.append("它做什么/机制")
    for heading in ("怎么用", "例子"):
        if not has_heading(md, heading):
            missing.append(heading)
    return "完整" if not missing else "缺章节"


def missing_sections(md: str) -> list[str]:
    if (parse_generation(md) or "").startswith("seed-adopted"):
        return []
    out = []
    if not (has_heading(md, "它做什么") or has_heading(md, "机制")):
        out.append("它做什么/机制")
    for heading in ("怎么用", "例子"):
        if not has_heading(md, heading):
            out.append(heading)
    return out


def parse_generation(md: str) -> str | None:
    match = GENERATION_RE.search(md or "")
    return match.group(1).strip() if match else None


def parse_evidence_status(md: str) -> str:
    match = EVIDENCE_RE.search(md or "")
    return match.group(1).strip() if match else "未标记"


def would_use_web(project_root: Path, term: str, extra_needles: list[str] | None = None) -> bool:
    if aliases.resolve(project_root, term):
        return False
    excerpts = kw.excerpts_for(project_root, term, extra_needles=extra_needles)
    return not kw.coverage_sufficient(excerpts, term)


def wiki_files(project_root: Path) -> list[Path]:
    return kw.iter_articles(project_root)


def backlinks(project_root: Path, rel: str) -> list[dict]:
    found = []
    for path in wiki_files(project_root):
        other = kw.rel_article(project_root, path)
        if other == rel:
            continue
        md = path.read_text(encoding="utf-8")
        for _label, href in MD_LINK_RE.findall(md):
            target = resolve_href(other, href)
            if target == rel:
                found.append({"path": other, "title": kw.parse_title(md) or path.stem})
                break
    return found


def _already_links_to(md: str, from_rel: str, to_rel: str) -> bool:
    for _label, href in MD_LINK_RE.findall(md):
        if resolve_href(from_rel, href) == to_rel:
            return True
    return False


def article_body(md: str) -> str:
    """Return prose used for semantic lint, excluding metadata and link destinations."""
    body = re.sub(r"^>.*$", "", md, flags=re.M)
    body = MD_LINK_RE.sub("", body)
    return body


def mentions_title(md: str, title: str) -> bool:
    body = article_body(md)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+-]*", title):
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(title)}(?![A-Za-z0-9_])"
        return bool(re.search(pattern, body, re.I))
    return title.casefold() in body.casefold()


def article_quality_issues(
    project_root: Path,
    rel: str,
    md: str,
    *,
    extra_paths: set[str] | None = None,
) -> list[str]:
    problems: list[str] = []
    missing = missing_sections(md)
    if missing:
        problems.append("缺少章节：" + "、".join(missing))
    status = parse_status(md)
    if not status:
        problems.append("缺少内容状态")
    elif status not in VALID_STATUSES:
        problems.append(f"非法内容状态：{status}")
    if not re.search(r"^>\s*(?:Sources|Raw):\s*\S", md, re.M | re.I):
        problems.append("缺少 Sources 或 Raw")
    available = extra_paths or set()
    for label, href in MD_LINK_RE.findall(md):
        if href.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = resolve_href(rel, href)
        if not target:
            continue
        path = project_root / target if target.startswith("raw/") else project_root / "wiki" / target
        if target not in available and not path.is_file():
            problems.append(f"死链：{label} -> {href}")
    return problems


def add_see_also(md: str, from_rel: str, to_rel: str, title: str) -> str:
    if _already_links_to(md, from_rel, to_rel):
        return md
    link = f"[{title}]({rel_href(from_rel, to_rel)})"
    if re.search(r"^##\s+See Also\s*$", md, re.M):
        return md.rstrip() + f"\n- {link}\n"
    return md.rstrip() + f"\n\n## See Also\n\n- {link}\n"


def update_referring_pages(project_root: Path, term: str, dest_rel: str, title: str) -> list[str]:
    updated: list[str] = []
    needle = term.strip().lower()
    for path in wiki_files(project_root):
        rel = kw.rel_article(project_root, path)
        if rel == dest_rel:
            continue
        md = path.read_text(encoding="utf-8")
        body = md.lower()
        if needle not in body and title.lower() not in body:
            continue
        new = add_see_also(md, rel, dest_rel, title)
        if new != md:
            path.write_text(new, encoding="utf-8")
            updated.append(rel)
    return updated


def mention_counts(project_root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    catalog = [row for row in kw.collect_keywords(project_root) if not row["path"] and len(row["term"]) >= 2]
    for path in wiki_files(project_root):
        text = path.read_text(encoding="utf-8").lower()
        seen: set[str] = set()
        for row in catalog:
            term = row["term"]
            key = term.lower()
            if key in seen:
                continue
            if key in text:
                counts[term] = counts.get(term, 0) + 1
                seen.add(key)
    return counts


def mentioned_but_missing(project_root: Path, min_mentions: int = 2) -> list[dict]:
    rows = []
    title_index = aliases.build_title_index(project_root)
    for term, count in mention_counts(project_root).items():
        if count < min_mentions:
            continue
        if kw.is_skippable(term) or term in {
            "解释",
            "依据",
            "注意点",
            "例子",
            "公式",
            "生成",
            "理解",
            "反例",
            "输入",
            "输出",
            "它做什么",
            "怎么用",
        }:
            continue
        if aliases.norm(term) in title_index:
            continue
        excerpts = kw.excerpts_for(project_root, term, limit=8)
        rows.append(
            {
                "term": term,
                "mentions": count,
                "sources": [
                    {"path": item["path"], "title": item["title"], "heading": "", "passage": item["text"]}
                    for item in excerpts
                ],
            }
        )
    rows.sort(key=lambda r: (-r["mentions"], r["term"]))
    return rows


def fix_missing_backlinks(project_root: Path) -> list[str]:
    articles = []
    for path in wiki_files(project_root):
        rel = kw.rel_article(project_root, path)
        md = path.read_text(encoding="utf-8")
        title = kw.parse_title(md) or path.stem
        articles.append((rel, title, md, path))
    titles = [(rel, title) for rel, title, _md, _path in articles]
    updated: list[str] = []
    for rel_b, _title_b, md_b, path in articles:
        new = md_b
        for rel_a, title_a in titles:
            if rel_a == rel_b or len(title_a) < 2:
                continue
            if title_a.lower() not in md_b.lower():
                continue
            if _already_links_to(new, rel_b, rel_a):
                continue
            new = add_see_also(new, rel_b, rel_a, title_a)
        if new != md_b:
            path.write_text(new, encoding="utf-8")
            updated.append(rel_b)
    return updated


def highlightable_keywords(project_root: Path) -> list[dict]:
    missing_ok = {row["term"] for row in mentioned_but_missing(project_root, min_mentions=2)}
    for path in kw.iter_articles(project_root):
        md = path.read_text(encoding="utf-8")
        if (parse_generation(md) or "").startswith("seed-adopted"):
            missing_ok.update(kw.extract_from_markdown(md))
    out = []
    for row in kw.collect_keywords(project_root):
        if row["path"]:
            out.append({**row, "kind": "page"})
        elif row["term"] in missing_ok:
            out.append({**row, "kind": "missing"})
    return out


def lint_wiki(project_root: Path) -> list[dict]:
    issues: list[dict] = []
    articles = []
    for path in wiki_files(project_root):
        rel = kw.rel_article(project_root, path)
        md = path.read_text(encoding="utf-8")
        title = kw.parse_title(md) or path.stem
        articles.append((rel, title, md))
        if not cats.parse_category_line(md):
            issues.append({"id": f"missing_category:{rel}", "kind": "missing_category", "path": rel, "title": title, "detail": title})
        quality = article_quality_issues(project_root, rel, md)
        quality = [item for item in quality if not item.startswith("死链：")]
        if quality:
            issues.append(
                {
                    "id": f"content_quality:{rel}",
                    "kind": "content_quality",
                    "path": rel,
                    "title": title,
                    "detail": "；".join(quality),
                }
            )
        for label, href in MD_LINK_RE.findall(md):
            if href.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = resolve_href(rel, href)
            if not target:
                continue
            if target.startswith("raw/"):
                if not (project_root / target).is_file():
                    issues.append(
                        {"id": f"dead_link:{rel}:{href}", "kind": "dead_link", "path": rel, "title": title, "target": href, "detail": f"{label} -> {href}"}
                    )
                continue
            dest = project_root / "wiki" / target
            if not dest.is_file():
                issues.append({"id": f"dead_link:{rel}:{href}", "kind": "dead_link", "path": rel, "title": title, "target": href, "detail": f"{label} -> {href}"})

    titles = [(rel, title) for rel, title, _md in articles]
    for rel_b, title_b, md_b in articles:
        body = md_b.lower()
        for rel_a, title_a in titles:
            if rel_a == rel_b or len(title_a) < 2:
                continue
            if not mentions_title(md_b, title_a):
                continue
            if _already_links_to(md_b, rel_b, rel_a):
                continue
            issues.append(
                {
                    "id": f"missing_backlink:{rel_b}:{rel_a}",
                    "kind": "missing_backlink",
                    "path": rel_b,
                    "title": title_b,
                    "target": rel_a,
                    "detail": f"mentions {title_a} but does not link to {rel_a}",
                }
            )

    for i, (rel_a, title_a, _md_a) in enumerate(articles):
        for rel_b, title_b, _md_b in articles[i + 1 :]:
            if aliases.norm(title_a) == aliases.norm(title_b):
                issues.append(
                    {
                        "id": f"near_duplicate:{rel_a}:{rel_b}",
                        "kind": "near_duplicate",
                        "path": rel_a,
                        "title": title_a,
                        "target": rel_b,
                        "detail": f"{title_a} ~ {title_b} ({rel_b})",
                    }
                )
                continue
            ratio = SequenceMatcher(None, aliases.norm(title_a), aliases.norm(title_b)).ratio()
            if ratio >= 0.86:
                issues.append(
                    {
                        "id": f"near_duplicate:{rel_a}:{rel_b}",
                        "kind": "near_duplicate",
                        "path": rel_a,
                        "title": title_a,
                        "target": rel_b,
                        "detail": f"{title_a} ~ {title_b} ({rel_b})",
                    }
                )
    return issues


def rewrite_wiki_hrefs(md: str, from_rel: str, old_rel: str, new_rel: str) -> str:
    def repl(match: re.Match) -> str:
        label, href = match.group(1), match.group(2)
        target = resolve_href(from_rel, href)
        if target == old_rel:
            return f"[{label}]({rel_href(from_rel, new_rel)})"
        return match.group(0)

    return MD_LINK_RE.sub(repl, md)


def rebase_wiki_hrefs(md: str, old_rel: str, new_rel: str) -> str:
    """Keep a moved page's relative links pointing at the same canonical files."""

    def repl(match: re.Match) -> str:
        label, href = match.group(1), match.group(2)
        if href.startswith(("http://", "https://", "mailto:", "#")):
            return match.group(0)
        fragment = ""
        if "#" in href:
            href, fragment = href.split("#", 1)
            fragment = "#" + fragment
        target = resolve_href(old_rel, href)
        if not target:
            return match.group(0)
        return f"[{label}]({rel_href(new_rel, target)}{fragment})"

    return MD_LINK_RE.sub(repl, md)


def relocate_article(project_root: Path, old_rel: str, new_rel: str) -> str:
    if old_rel == new_rel:
        return old_rel
    src = project_root / "wiki" / old_rel
    dest = project_root / "wiki" / new_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    src.unlink()
    index = project_root / "wiki" / "index.md"
    if index.exists():
        text = index.read_text(encoding="utf-8")
        text = text.replace(f"]({old_rel})", f"]({new_rel})")
        index.write_text(text, encoding="utf-8")
    for path in wiki_files(project_root):
        rel = kw.rel_article(project_root, path)
        md = path.read_text(encoding="utf-8")
        new = rewrite_wiki_hrefs(md, rel, old_rel, new_rel)
        if new != md:
            path.write_text(new, encoding="utf-8")
    return new_rel


def safe_wiki_rel(rel: str) -> str:
    text = (rel or "").strip()
    if not text or text.startswith(("/", "\\")) or ".." in Path(text).parts or ".." in text:
        raise ValueError("invalid path")
    return text


def apply_meta(
    project_root: Path,
    rel: str,
    category: str | None = None,
    status: str | None = None,
) -> str:
    rel = safe_wiki_rel(rel)
    wiki_root = (project_root / "wiki").resolve()
    path = (wiki_root / rel).resolve()
    if not path.is_relative_to(wiki_root):
        raise ValueError("invalid path")
    if not path.is_file():
        raise FileNotFoundError(rel)
    md = path.read_text(encoding="utf-8")
    if status is not None:
        md = ensure_status_header(md, status)
    if category:
        md = cats.ensure_category_header(md, category)
        folder = Path(rel).parent.name
        if folder != category and category in cats.BY_ID:
            path.write_text(md, encoding="utf-8")
            new_rel = f"{category}/{path.name}"
            n = 2
            while new_rel != rel and (project_root / "wiki" / new_rel).exists():
                new_rel = f"{category}/{path.stem}-{n}{path.suffix}"
                n += 1
            return relocate_article(project_root, rel, new_rel)
    path.write_text(md, encoding="utf-8")
    return rel


def rebuild_index(project_root: Path) -> None:
    grouped: dict[str, list[tuple[str, str, str]]] = {cid: [] for cid in cats.ORDER}
    for path in wiki_files(project_root):
        rel = kw.rel_article(project_root, path)
        md = path.read_text(encoding="utf-8")
        title = kw.parse_title(md) or path.stem
        category = cats.category_for_article(md, rel)
        mark = completeness(md)
        summary = f"{cats.label_of(category)} · {title}"
        if mark != "词条":
            summary = f"[{mark}] {summary}"
        grouped.setdefault(category, []).append((title, rel, summary))
    chunks = ["# Knowledge Base Index\n"]
    for cid, label, blurb, _hints in cats.CATALOG:
        rows = grouped.get(cid) or []
        if not rows and cid == "concepts":
            continue
        if not rows and cid not in {p.parent.name for p in wiki_files(project_root)}:
            # still show categories that have pages via metadata
            if not rows:
                continue
        chunks.append(f"\n## {cid}\n\n{blurb}\n\n")
        chunks.append("| Article | Summary | Updated |\n|---------|---------|---------|\n")
        for title, rel, summary in sorted(rows, key=lambda r: r[0]):
            chunks.append(f"| [{title}]({rel}) | {summary} | {date.today().isoformat()} |\n")
    (project_root / "wiki" / "index.md").write_text("".join(chunks), encoding="utf-8")


def mark_thin_pages(project_root: Path) -> list[str]:
    touched = []
    for path in wiki_files(project_root):
        md = path.read_text(encoding="utf-8")
        if completeness(md) != "草稿":
            continue
        if parse_status(md) == "草稿":
            continue
        path.write_text(ensure_status_header(md, "草稿"), encoding="utf-8")
        touched.append(kw.rel_article(project_root, path))
    return touched
