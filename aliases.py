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
    for alias, canonical in SEED_ALIASES.items():
        target = index.get(norm(canonical))
        if target:
            index.setdefault(norm(alias), target)
    return index


def resolve(project_root: Path, term: str) -> str | None:
    if not term or not term.strip():
        return None
    return build_title_index(project_root).get(norm(term))
