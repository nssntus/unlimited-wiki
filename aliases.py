"""One concept, one page. Aliases resolve to a canonical article."""

from __future__ import annotations

import re
from pathlib import Path

# alias -> canonical title (must match an existing article H1)
SEED_ALIASES = {
    "model context protocol": "MCP",
    "mcp（model context protocol）": "MCP",
    "skill": "Skills",
    "skills": "Skills",
    "四要素万能公式": "四要素",
    "万能公式": "四要素",
    "prompt 四要素": "四要素",
    "chain of thought": "思维链",
    "cot": "思维链",
    "分步思考": "思维链",
    "custom instructions": "自定义指令",
    "系统指令": "自定义指令",
    "triggered automation": "触发式自动化",
    "scheduled tasks": "定时任务",
    "能力跃迁": "普通人 AI 能力跃迁路径",
    "chatgpt": "ChatGPT",
}

ALIAS_RE = re.compile(r"^>\s*Aliases:\s*(.+)$", re.M | re.I)
REDIRECT_RE = re.compile(r"^>\s*Redirect:\s*(.+?)\s*$", re.M | re.I)


def norm(term: str) -> str:
    text = re.sub(r"\s+", " ", (term or "").strip())
    return text.lower()


def parse_aliases(md: str) -> list[str]:
    m = ALIAS_RE.search(md or "")
    if not m:
        return []
    parts = re.split(r"[;；,，]", m.group(1))
    return [p.strip() for p in parts if p.strip()]


def ensure_aliases_header(md: str, aliases: list[str]) -> str:
    current = parse_aliases(md)
    merged: list[str] = []
    for item in [*current, *aliases]:
        if item and item not in merged:
            merged.append(item)
    line = f"> Aliases: {'; '.join(merged)}"
    if ALIAS_RE.search(md):
        return ALIAS_RE.sub(line, md, count=1)
    lines = md.splitlines()
    insert_at = 0
    for i, row in enumerate(lines):
        if row.startswith("# "):
            insert_at = i + 1
            while insert_at < len(lines) and lines[insert_at] == "":
                insert_at += 1
            break
    # keep with other > metadata
    lines.insert(insert_at, line)
    return "\n".join(lines) + ("\n" if md.endswith("\n") else "")


def build_title_index(project_root: Path) -> dict[str, str]:
    """norm(title or alias) -> article path relative to wiki/."""
    from keywords import iter_articles, parse_title, rel_article

    index: dict[str, str] = {}
    for path in iter_articles(project_root):
        md = path.read_text(encoding="utf-8")
        rel = rel_article(project_root, path)
        title = parse_title(md) or path.stem
        index[norm(title)] = rel
        for alias in parse_aliases(md):
            index.setdefault(norm(alias), rel)
    # Redirect pages are intentionally excluded from normal article scans, but
    # their former titles and aliases remain valid canonical lookups.
    wiki_root = project_root / "wiki"
    for path in sorted(wiki_root.rglob("*.md")) if wiki_root.exists() else []:
        if path.name in {"index.md", "log.md"}:
            continue
        md = path.read_text(encoding="utf-8")
        match = REDIRECT_RE.search(md[:4096])
        if not match:
            continue
        source_rel = path.relative_to(wiki_root).as_posix()
        target = _resolve_redirect_rel(source_rel, match.group(1).strip())
        final = follow_redirect(project_root, target)
        title = next((line[2:].strip() for line in md.splitlines() if line.startswith("# ")), path.stem)
        index.setdefault(norm(title), final)
        for alias in parse_aliases(md):
            index.setdefault(norm(alias), final)
    for alias, canonical in SEED_ALIASES.items():
        target = index.get(norm(canonical))
        if target:
            index.setdefault(norm(alias), target)
    return index


def resolve(project_root: Path, term: str) -> str | None:
    if not term or not term.strip():
        return None
    return build_title_index(project_root).get(norm(term))


def _resolve_redirect_rel(source_rel: str, target: str) -> str:
    if target.startswith("/") or "\\" in target:
        raise ValueError("invalid redirect")
    parts = list(Path(source_rel).parent.parts)
    for piece in Path(target).parts:
        if piece == "..":
            if not parts:
                raise ValueError("redirect escapes wiki")
            parts.pop()
        elif piece not in {"", "."}:
            parts.append(piece)
    return "/".join(parts)


def follow_redirect(project_root: Path, rel: str, *, max_hops: int = 16) -> str:
    wiki_root = (project_root / "wiki").resolve()
    current = rel
    seen: set[str] = set()
    for _ in range(max_hops):
        if current in seen:
            raise ValueError("redirect cycle")
        seen.add(current)
        path = (wiki_root / current).resolve()
        if not path.is_relative_to(wiki_root) or not path.is_file():
            return current
        md = path.read_text(encoding="utf-8")
        match = REDIRECT_RE.search(md[:4096])
        if not match:
            return current
        current = _resolve_redirect_rel(current, match.group(1).strip())
    raise ValueError("redirect chain is too long")
